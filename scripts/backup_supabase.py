#!/usr/bin/env python3
"""One-time public-schema backup. Production operations are strictly read-only.

Only an RSA/AES encrypted archive is eligible for upload. Plaintext is confined
to a private runner temp directory and removed in finally. The private key is
never required on the runner or in the repository.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b'TFPGBAK1\n'
KEY_LABEL = b'tour-finder-backup-v1'
POSTGRES_IMAGE = 'postgres:17'
REFERENCE_DATE = '2026-10-08'
CHUNK = 1024 * 1024


class BackupFailure(Exception):
    """Only static, non-secret descriptions are passed to this exception."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def encrypt_bundle(bundle: Path, public_key_path: Path, destination: Path) -> dict:
    public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 3072:
        raise BackupFailure('public key must be RSA, at least 3072 bits')
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    key, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
    wrapped = public_key.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=KEY_LABEL))
    header = {
        'format': 'tour-finder-backup-v1',
        'encryption': 'AES-256-GCM',
        'key_wrap': 'RSA-OAEP-SHA256',
        'public_key_sha256': hashlib.sha256(der).hexdigest(),
        'encrypted_key_b64': base64.b64encode(wrapped).decode('ascii'),
        'nonce_b64': base64.b64encode(nonce).decode('ascii'),
    }
    encoded = json.dumps(header, sort_keys=True, separators=(',', ':')).encode('utf-8')
    authenticated_header = MAGIC + struct.pack('>I', len(encoded)) + encoded
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(authenticated_header)
    partial = destination.with_suffix(destination.suffix + '.part')
    if destination.exists():
        raise BackupFailure('encrypted destination already exists')
    try:
        with bundle.open('rb') as source, partial.open('xb') as target:
            os.chmod(partial, 0o600)
            target.write(authenticated_header)
            for chunk in iter(lambda: source.read(CHUNK), b''):
                target.write(encryptor.update(chunk))
            target.write(encryptor.finalize())
            target.write(encryptor.tag)
            target.flush()
            os.fsync(target.fileno())
        partial.rename(destination)
    finally:
        if partial.exists():
            partial.unlink()
    return {'archive_sha256': file_sha256(destination), 'archive_bytes': destination.stat().st_size, 'public_key_sha256': header['public_key_sha256']}


def run_checked(args: list[str], *, stage: str, env: dict, timeout: int = 300, deadline: float | None = None) -> bytes:
    if deadline is not None:
        timeout = min(timeout, max(1, int(deadline - time.monotonic())))
    try:
        result = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise BackupFailure(stage + ': timeout') from None
    except OSError:
        raise BackupFailure(stage + ': could not start subprocess') from None
    if result.returncode:
        # Never print subprocess output: PostgreSQL failures can contain DSNs,
        # credentials, SQL data, or user content.
        raise BackupFailure(stage + ': process failed (exit ' + str(result.returncode) + ')')
    return result.stdout


def source_pg_env(connection_url: str, base_env: dict) -> dict:
    parts = conninfo_to_dict(connection_url)
    mapping = {'host': 'PGHOST', 'port': 'PGPORT', 'user': 'PGUSER', 'password': 'PGPASSWORD', 'dbname': 'PGDATABASE', 'sslmode': 'PGSSLMODE'}
    env = {key: value for key, value in base_env.items() if not key.startswith('PG')}
    for key, name in mapping.items():
        if key in parts:
            env[name] = parts[key]
    if not env.get('PGHOST') or not env.get('PGUSER') or not env.get('PGPASSWORD'):
        raise BackupFailure('DATABASE_URL must include host, user and password')
    env.setdefault('PGPORT', '5432')
    env.setdefault('PGDATABASE', 'postgres')
    env.setdefault('PGSSLMODE', 'require')
    if env['PGSSLMODE'] not in {'require', 'verify-ca', 'verify-full'}:
        raise BackupFailure('source connection must require TLS')
    env['PGCONNECT_TIMEOUT'] = '20'
    env['PGOPTIONS'] = '-c default_transaction_read_only=on -c statement_timeout=900000 -c lock_timeout=5000'
    return env


def docker_source_dump(snapshot: str, work: Path, env: dict, deadline: float) -> None:
    args = ['docker', 'run', '--rm', '--network', 'host', '--user', f'{os.getuid()}:{os.getgid()}', '--volume', str(work) + ':/backup']
    for name in ('PGHOST', 'PGPORT', 'PGUSER', 'PGPASSWORD', 'PGDATABASE', 'PGSSLMODE', 'PGCONNECT_TIMEOUT', 'PGOPTIONS'):
        args += ['--env', name]
    args += [POSTGRES_IMAGE, 'pg_dump', '--format=custom', '--compress=6', '--schema=public', '--no-owner', '--no-acl', '--snapshot=' + snapshot, '--lock-wait-timeout=5s', '--file=/backup/public-schema.dump']
    run_checked(args, stage='production pg_dump', env=env, timeout=900, deadline=deadline)


def source_snapshot_dump(url: str, work: Path, env: dict, deadline: float) -> dict:
    with psycopg.connect(url, autocommit=True, connect_timeout=20, prepare_threshold=None, sslmode=env['PGSSLMODE'], application_name='tour-finder-readonly-backup') as connection:
        with connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            connection.execute("SET LOCAL statement_timeout = '180s'")
            connection.execute("SET LOCAL lock_timeout = '5s'")
            connection.execute("SET LOCAL idle_in_transaction_session_timeout = '25min'")
            version, major, readonly = connection.execute("SELECT current_setting('server_version'), current_setting('server_version_num')::integer / 10000, current_setting('transaction_read_only')").fetchone()
            if major != 17 or readonly != 'on':
                raise BackupFailure('source must be PostgreSQL 17 with read-only transaction')
            snapshot = connection.execute('SELECT pg_export_snapshot()').fetchone()[0]
            snapshot_created_at = datetime.now(timezone.utc).isoformat()
            tables = [row[0] for row in connection.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname")]
            if not tables or 'price_snapshots' not in tables or 'offers' not in tables:
                raise BackupFailure('expected application tables absent from source')
            counts = {}
            for table in tables:
                counts[table] = connection.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier('public'), sql.Identifier(table))).fetchone()[0]
            # The exporting transaction remains open until pg_dump finishes.
            docker_source_dump(snapshot, work, env, deadline)
            return {'postgres_server_version': version, 'isolation': 'REPEATABLE READ, READ ONLY, exported snapshot shared with pg_dump', 'snapshot_created_at_utc': snapshot_created_at, 'source_table_counts': counts}


def verify_restored(work: Path, expected: dict, base_env: dict, deadline: float) -> dict:
    name = 'tf-backup-verify-' + secrets.token_hex(6)
    env = {key: value for key, value in base_env.items() if not key.startswith('PG')}
    env['POSTGRES_PASSWORD'] = secrets.token_urlsafe(36)
    env['POSTGRES_DB'] = 'backup_verify'
    env['PGPASSWORD'] = env['POSTGRES_PASSWORD']
    created = False
    try:
        run_checked(['docker', 'run', '--detach', '--name', name, '--network', 'none', '--env', 'POSTGRES_PASSWORD', '--env', 'POSTGRES_DB', '--volume', str(work) + ':/backup:ro', POSTGRES_IMAGE], stage='start isolated restore database', env=env, deadline=deadline)
        created = True
        for _ in range(40):
            ready = subprocess.run(['docker', 'exec', name, 'pg_isready', '--username=postgres', '--dbname=backup_verify'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise BackupFailure('isolated restore database did not become ready')
        # pg_restore only targets a Unix socket INSIDE the no-network container.
        run_checked(['docker', 'exec', '--env', 'PGPASSWORD', name, 'pg_restore', '--exit-on-error', '--clean', '--if-exists', '--no-owner', '--no-acl', '--username=postgres', '--dbname=backup_verify', '/backup/public-schema.dump'], stage='restore into isolated database', env=env, timeout=600, deadline=deadline)

        def local_query(query: str):
            raw = run_checked(['docker', 'exec', '--env', 'PGPASSWORD', name, 'psql', '--no-psqlrc', '--username=postgres', '--dbname=backup_verify', '--tuples-only', '--no-align', '--set=ON_ERROR_STOP=1', '--command', query], stage='validate isolated database', env=env, timeout=180, deadline=deadline)
            return json.loads(raw)

        tables = local_query("SELECT json_agg(c.relname ORDER BY c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p')")
        if tables != sorted(expected):
            raise BackupFailure('restored table set differs from source snapshot')
        restored_counts = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            restored_counts[table] = local_query('SELECT count(*) FROM public.' + quoted)
        if restored_counts != expected:
            raise BackupFailure('restored row counts differ from source snapshot')
        # Additional archival-policy diagnostics run ONLY on the restored copy.
        stats = local_query("""SELECT json_build_object(
            'reference_date', '2026-10-08',
            'expired_offers', (SELECT count(*) FROM public.offers WHERE date_start < '2026-10-08'),
            'current_or_future_offers', (SELECT count(*) FROM public.offers WHERE date_start >= '2026-10-08'),
            'snapshots_for_expired_offers', (SELECT count(*) FROM public.price_snapshots s JOIN public.offers o ON o.id=s.offer_id WHERE o.date_start < '2026-10-08'),
            'first_snapshot_utc', (SELECT min(fetched_at) FROM public.price_snapshots),
            'last_snapshot_utc', (SELECT max(fetched_at) FROM public.price_snapshots),
            'first_departure', (SELECT min(date_start) FROM public.offers),
            'last_departure', (SELECT max(date_start) FROM public.offers),
            'snapshots_without_offer', (SELECT count(*) FROM public.price_snapshots s LEFT JOIN public.offers o ON o.id=s.offer_id WHERE o.id IS NULL),
            'snapshots_without_run', (SELECT count(*) FROM public.price_snapshots s LEFT JOIN public.fetch_runs r ON r.id=s.run_id WHERE s.run_id IS NOT NULL AND r.id IS NULL),
            'snapshots_with_null_run', (SELECT count(*) FROM public.price_snapshots WHERE run_id IS NULL)
        )""")
        return {'isolated_restore': 'passed', 'restored_table_counts': restored_counts, 'archive_policy_stats_from_restored_copy': stats}
    finally:
        if created:
            subprocess.run(['docker', 'rm', '--force', '--volumes', name], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)


RESTORE_INSTRUCTIONS = '''# Tour Finder — verified PUBLIC schema backup

This is a consistent PostgreSQL 17 custom-format dump of the production PUBLIC
schema. It includes Tour Finder tables, their data, schema objects and sequences.
Object ownership and GRANT/REVOKE privileges are intentionally omitted; this is
not a backup of database roles or the complete production permission setup.
It does NOT include Supabase Auth/Storage schemas, uploaded Storage files, service
settings, secrets, external projects or database roles.

The workflow restored this dump into an isolated PostgreSQL 17 instance and
compared every public table's row count with the same production snapshot.
The manifest records the dump SHA256, counts and verification results.

Keep the encrypted .tfbackup file AND its separately stored RSA private key.
Use the local decrypt_tour_finder_backup.py utility to authenticate/decrypt it.
The private key was never uploaded to GitHub or included in this archive.

To restore, provision and confirm a NEW empty PostgreSQL 17 database. Verify the
SHA256 of public-schema.dump against manifest.json, then use pg_restore
--exit-on-error --clean --if-exists --no-owner --no-acl
--dbname=NEW_EMPTY_DATABASE public-schema.dump. Supply
credentials using PGHOST/PGPORT/PGUSER/PGPASSWORD environment variables.
Never point this command at a production database without a separate migration
and rollback plan. Do not use --clean against an existing database.
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--public-key', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    deadline = time.monotonic() + 27 * 60
    os.umask(0o077)
    url = os.environ.pop('DATABASE_URL', '').replace('postgresql+psycopg://', 'postgresql://', 1)
    if not url.startswith(('postgresql://', 'postgres://')):
        raise BackupFailure('DATABASE_URL missing or unsupported')
    base_env = dict(os.environ)
    source_env = source_pg_env(url, base_env)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='tour-finder-plaintext-', dir=os.environ.get('RUNNER_TEMP')))
    try:
        print('Preparing official PostgreSQL 17 client and isolated restore image.', flush=True)
        run_checked(['docker', 'pull', POSTGRES_IMAGE], stage='pull official PostgreSQL image', env=base_env, timeout=300, deadline=deadline)
        print('Opening read-only consistent snapshot and exporting public schema.', flush=True)
        snapshot_info = source_snapshot_dump(url, work, source_env, deadline)
        dump_path = work / 'public-schema.dump'
        dump_hash = file_sha256(dump_path)
        print('Export finished; production connection closed. Restoring only into isolated runner database.', flush=True)
        validation = verify_restored(work, snapshot_info['source_table_counts'], base_env, deadline)
        manifest = {
            'backup_scope': 'Production PostgreSQL PUBLIC schema only; excludes ownership/ACL, other Supabase schemas and service/storage files',
            'project': 'S1rt3ge/tour-finder',
            'created_at_utc': datetime.now(timezone.utc).isoformat(),
            'dump_file': dump_path.name, 'dump_format': 'PostgreSQL 17 custom format',
            'dump_sha256': dump_hash, 'dump_bytes': dump_path.stat().st_size,
            **snapshot_info, 'validation': validation,
        }
        (work / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        (work / 'RESTORE.md').write_text(RESTORE_INSTRUCTIONS, encoding='utf-8')
        bundle = work / 'bundle.tar'
        with tarfile.open(bundle, 'w') as archive:
            for name in ('public-schema.dump', 'manifest.json', 'RESTORE.md'):
                archive.add(work / name, arcname=name, recursive=False)
        run_id = os.environ.get('GITHUB_RUN_ID', 'local')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', run_id):
            raise BackupFailure('invalid run identifier')
        destination = args.output_dir / ('tour-finder-supabase-public-2026-10-08-' + run_id + '.tfbackup')
        encrypted_info = encrypt_bundle(bundle, args.public_key, destination)
        print(json.dumps({'status': 'VERIFIED_AND_ENCRYPTED', 'archive_file': destination.name, **encrypted_info, 'table_counts': snapshot_info['source_table_counts'], 'restored_archive_statistics': validation['archive_policy_stats_from_restored_copy']}, sort_keys=True), flush=True)
        return 0
    finally:
        # Only the private tempfile directory created by this process is removed.
        shutil.rmtree(work)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except BackupFailure as exc:
        print('BACKUP FAILED: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        # Tracebacks and exception messages may include connection credentials.
        print('BACKUP FAILED: ' + type(exc).__name__ + ' (details suppressed to protect credentials)', file=sys.stderr)
        raise SystemExit(1)
