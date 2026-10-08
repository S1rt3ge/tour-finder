"""Versioned, public-tour-only SQLite projections and authenticated envelopes.

No application database is opened here. Exporters call the create_* helpers
against new temporary files, fill the explicit schemas, then encrypt_sqlite.
The recovery backup's RSA key and format are deliberately unrelated.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import tempfile
import time
import zlib

FORMAT = "tour-finder-read-archive-v1"
POINTER_FORMAT = "tour-finder-archive-pointer-v1"
MAGIC = b"TFREAD1\n"
MIB = 1024 * 1024
CATALOG_LIMIT = 320 * MIB
SHARD_LIMIT = 32 * MIB
CHUNK = MIB
SHARDS = 64
VALIDATION_CACHE_KIB = 64 * 1024
_OFFER_IDENTITY_COLUMNS = (
    "source", "source_hotel_id", "origin_id", "date_start", "nights", "board_code",
    "room_code", "room_placement", "pax_adl", "pax_chd", "children_ages",
)
HEX = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ArchiveError(RuntimeError):
    """Only fixed, non-sensitive error codes are exposed to callers."""


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def key_from_string(value: str) -> bytes:
    try:
        raw = bytes.fromhex(value) if HEX.fullmatch(value) else base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ArchiveError("archive_key_invalid") from None
    if len(raw) != 32:
        raise ArchiveError("archive_key_invalid")
    return raw


def normalize_offer_key(value: str) -> str:
    value = value[2:] if isinstance(value, str) and value.startswith("a_") else value
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ArchiveError("offer_key_invalid")
    return value


def offer_key(row) -> str:
    """Natural identity, independent of recyclable DB IDs and mutable labels."""
    try:
        ages = sorted(int(v.strip()) for v in str(row.get("children_ages") or "").split(",") if v.strip())
        identity = [str(row[name]) for name in ("source", "source_hotel_id", "origin_id", "date_start")]
        date.fromisoformat(identity[-1])
        identity += [int(row["nights"]), str(row["board_code"]),
                     str(row.get("room_code") or ""), str(row.get("room_placement") or ""),
                     int(row["pax_adl"]), int(row.get("pax_chd") or 0), ages]
        if any(value in ("", "None") for value in identity[:4]) or identity[4] <= 0:
            raise ValueError()
        if identity[8] <= 0 or identity[9] != len(ages) or any(v < 0 or v > 17 for v in ages):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise ArchiveError("offer_identity_invalid") from None
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def history_shard(key: str) -> int:
    return int(normalize_offer_key(key), 16) % SHARDS


def observation_key(key: str, row) -> str:
    """Fingerprint of an observation; snapshot/run IDs are provenance only."""
    value = [normalize_offer_key(key), row["fetched_at"], row["price_cents"],
             row["currency"], row.get("is_hot", 0), row.get("availability"),
             row.get("stop_sale"), row.get("operator_avg_price_cents")]
    return hashlib.sha256(canonical_json(value)).hexdigest()


CATALOG_SCHEMA = """
CREATE TABLE hotels(source TEXT NOT NULL,source_hotel_id TEXT NOT NULL,name TEXT NOT NULL,
 category TEXT,country_id TEXT,country_name TEXT,city_name TEXT,latitude REAL,longitude REAL,
 photo_url TEXT,PRIMARY KEY(source,source_hotel_id));
CREATE TABLE offers(id INTEGER PRIMARY KEY,source TEXT NOT NULL,source_hotel_id TEXT NOT NULL,
 origin_id TEXT NOT NULL,origin_name TEXT,date_start TEXT NOT NULL,date_end TEXT,nights INTEGER NOT NULL,
 board_code TEXT NOT NULL,board_name TEXT,room_code TEXT NOT NULL DEFAULT '',room_name TEXT,
 room_placement TEXT NOT NULL DEFAULT '',pax_adl INTEGER NOT NULL,pax_chd INTEGER NOT NULL DEFAULT 0,
 children_ages TEXT NOT NULL DEFAULT '',operator TEXT,link TEXT,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL);
CREATE INDEX idx_offers_date ON offers(date_start,nights);
CREATE TABLE price_snapshots(id INTEGER PRIMARY KEY,offer_id INTEGER NOT NULL UNIQUE,run_id INTEGER,
 fetched_at TEXT NOT NULL,price_cents INTEGER NOT NULL,currency TEXT NOT NULL,is_hot INTEGER NOT NULL,
 availability TEXT,stop_sale TEXT,operator_avg_price_cents INTEGER);
CREATE INDEX idx_snapshots_latest ON price_snapshots(offer_id,fetched_at,id);
CREATE TABLE hotel_reviews(id INTEGER PRIMARY KEY,source TEXT NOT NULL,source_hotel_id TEXT NOT NULL,
 platform TEXT NOT NULL,rating REAL,rating_scale REAL NOT NULL,reviews_count INTEGER,summary TEXT,
 external_id TEXT,url TEXT,matched_name TEXT,match_status TEXT NOT NULL,fetched_at TEXT NOT NULL);
CREATE INDEX idx_reviews_hotel ON hotel_reviews(source,source_hotel_id);
CREATE TABLE archive_offer_keys(offer_id INTEGER PRIMARY KEY,offer_key TEXT NOT NULL UNIQUE);
"""
HISTORY_SCHEMA = """
CREATE TABLE archive_history(observation_key TEXT PRIMARY KEY,offer_key TEXT NOT NULL,
 snapshot_id INTEGER NOT NULL,run_id INTEGER,fetched_at TEXT NOT NULL,price_cents INTEGER NOT NULL,
 currency TEXT NOT NULL,is_hot INTEGER NOT NULL,availability TEXT,stop_sale TEXT,operator_avg_price_cents INTEGER);
CREATE INDEX idx_archive_history_offer ON archive_history(offer_key,fetched_at,snapshot_id,observation_key);
"""
META_SCHEMA = "CREATE TABLE archive_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);"


def _schema_columns(schema: str) -> dict[str, set[str]]:
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(schema + META_SCHEMA)
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')} for table in tables}


SCHEMAS = {"catalog": _schema_columns(CATALOG_SCHEMA), "history": _schema_columns(HISTORY_SCHEMA)}


def _context(dataset_id, kind, shard):
    if not isinstance(dataset_id, str) or not IDENTIFIER.fullmatch(dataset_id):
        raise ArchiveError("dataset_id_invalid")
    if kind not in SCHEMAS or (kind == "catalog" and shard is not None):
        raise ArchiveError("archive_kind_invalid")
    if kind == "history" and (type(shard) is not int or not 0 <= shard < SHARDS):
        raise ArchiveError("archive_shard_invalid")


def create_projection(path: Path, *, dataset_id: str, kind: str, shard=None, created_at=None):
    """Create a NEW writable export file. Caller inserts, commits and closes it."""
    _context(dataset_id, kind, shard)
    path = Path(path)
    with path.open("xb"):
        pass
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path)
    try:
        conn.executescript((CATALOG_SCHEMA if kind == "catalog" else HISTORY_SCHEMA) + META_SCHEMA)
        metadata = {"format": FORMAT, "dataset_id": dataset_id, "kind": kind,
                    "shard": "" if shard is None else str(shard),
                    "created_at": created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "stats_scope": "latest_only" if kind == "catalog" else "complete_history"}
        conn.executemany("INSERT INTO archive_metadata VALUES (?,?)", metadata.items())
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


def create_catalog(path, *, dataset_id, created_at=None):
    return create_projection(path, dataset_id=dataset_id, kind="catalog", created_at=created_at)


def create_history(path, *, dataset_id, shard, created_at=None):
    return create_projection(path, dataset_id=dataset_id, kind="history", shard=shard, created_at=created_at)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_readonly(path: Path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    conn.execute("PRAGMA cache_size=-8192")
    return conn


def validate_sqlite(path: Path, *, dataset_id: str, kind: str, shard=None, expected_counts=None) -> dict:
    _context(dataset_id, kind, shard)
    limit = CATALOG_LIMIT if kind == "catalog" else SHARD_LIMIT
    if Path(path).stat().st_size > limit:
        raise ArchiveError("archive_plaintext_too_large")
    conn = None
    try:
        conn = open_readonly(path)
        # Full integrity checking probes indexed rows across the catalog.
        # A bounded validation-only page cache avoids repeated random reads;
        # normal read connections retain their smaller 8 MiB query cache.
        conn.execute(f"PRAGMA cache_size=-{VALIDATION_CACHE_KIB}")
        deadline = time.monotonic() + 30
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        objects = conn.execute("SELECT name,type,sql FROM sqlite_master").fetchall()
        if any(row["type"] in {"view", "trigger"} or "VIRTUAL TABLE" in (row["sql"] or "").upper() for row in objects):
            raise ArchiveError("archive_schema_invalid")
        tables = {row["name"] for row in objects if row["type"] == "table"}
        if tables != set(SCHEMAS[kind]):
            raise ArchiveError("archive_schema_invalid")
        for table, columns in SCHEMAS[kind].items():
            actual = {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if actual != columns:
                raise ArchiveError("archive_schema_invalid")
        if [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ArchiveError("archive_integrity_failed")
        meta = dict(conn.execute("SELECT key,value FROM archive_metadata"))
        if (meta.get("format") != FORMAT or meta.get("dataset_id") != dataset_id
                or meta.get("kind") != kind or meta.get("shard") != ("" if shard is None else str(shard))):
            raise ArchiveError("archive_metadata_mismatch")
        if not meta.get("created_at"):
            raise ArchiveError("archive_metadata_mismatch")
        counts = {table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in sorted(tables)}
        if expected_counts is not None and counts != expected_counts:
            raise ArchiveError("archive_row_counts_mismatch")
        if kind == "catalog":
            if counts["offers"] != counts["archive_offer_keys"] or counts["offers"] != counts["price_snapshots"]:
                raise ArchiveError("archive_catalog_incomplete")
            missing = conn.execute("""SELECT 1 FROM offers o
                LEFT JOIN archive_offer_keys k ON k.offer_id=o.id
                LEFT JOIN price_snapshots p ON p.offer_id=o.id
                LEFT JOIN hotels h ON h.source=o.source AND h.source_hotel_id=o.source_hotel_id
                WHERE k.offer_key IS NULL OR p.id IS NULL OR h.source IS NULL LIMIT 1""").fetchone()
            if missing:
                raise ArchiveError("archive_catalog_incomplete")
            # sqlite3.Row -> dict performs name lookups for every column.
            # Stream only identity fields as tuples, preserving every natural
            # key check while avoiding the wide, quadratic row conversion.
            cursor = conn.cursor()
            cursor.row_factory = None
            identity_sql = ",".join("o." + name for name in _OFFER_IDENTITY_COLUMNS)
            for row in cursor.execute(f"SELECT {identity_sql},k.offer_key FROM offers o JOIN archive_offer_keys k ON k.offer_id=o.id"):
                if offer_key(dict(zip(_OFFER_IDENTITY_COLUMNS, row[:-1]))) != row[-1]:
                    raise ArchiveError("archive_offer_identity_mismatch")
        else:
            for row in conn.execute("SELECT * FROM archive_history"):
                if history_shard(row["offer_key"]) != shard or observation_key(row["offer_key"], dict(row)) != row["observation_key"]:
                    raise ArchiveError("archive_observation_identity_mismatch")
        return {"row_counts": counts, "created_at": meta["created_at"]}
    except sqlite3.Error:
        raise ArchiveError("archive_sqlite_invalid") from None
    finally:
        if conn is not None:
            conn.close()


def encrypt_sqlite(path, destination, *, key: bytes, dataset_id: str, kind: str, shard=None) -> dict:
    """Validate then gzip+AES-256-GCM; returns descriptor without its remote URL."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if len(key) != 32:
        raise ArchiveError("archive_key_invalid")
    path, destination = Path(path), Path(destination)
    if destination.exists():
        raise ArchiveError("archive_destination_exists")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ArchiveError("archive_database_not_finalized")
    info = validate_sqlite(path, dataset_id=dataset_id, kind=kind, shard=shard)
    header = {"format": FORMAT, "dataset_id": dataset_id, "kind": kind, "shard": shard,
              **info, "plaintext_bytes": path.stat().st_size, "plaintext_sha256": file_sha256(path),
              "nonce": base64.b64encode(os.urandom(12)).decode("ascii")}
    encoded = canonical_json(header)
    aad = MAGIC + struct.pack(">I", len(encoded)) + encoded
    encryptor = Cipher(algorithms.AES(key), modes.GCM(base64.b64decode(header["nonce"]))).encryptor()
    encryptor.authenticate_additional_data(aad)
    compressor = zlib.compressobj(level=6, wbits=31)
    created = False
    try:
        with destination.open("xb") as target, path.open("rb") as source:
            created = True
            os.chmod(destination, 0o600)
            target.write(aad)
            for chunk in iter(lambda: source.read(CHUNK), b""):
                target.write(encryptor.update(compressor.compress(chunk)))
            target.write(encryptor.update(compressor.flush()))
            target.write(encryptor.finalize())
            target.write(encryptor.tag)
    except Exception:
        if created and destination.exists():
            destination.unlink()
        raise
    return {key: value for key, value in header.items() if key not in {"nonce", "format"}} | {
        "sha256": file_sha256(destination), "bytes": destination.stat().st_size}


def decrypt_sqlite(encrypted, destination, *, key: bytes, descriptor: dict) -> dict:
    """Authenticate completely BEFORE decompression or opening any SQLite file."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    encrypted, destination = Path(encrypted), Path(destination)
    if destination.exists():
        raise ArchiveError("archive_destination_exists")
    if len(key) != 32 or encrypted.stat().st_size != descriptor["bytes"] or file_sha256(encrypted) != descriptor["sha256"]:
        raise ArchiveError("archive_ciphertext_mismatch")
    temporary = None
    created = False
    try:
        with encrypted.open("rb") as source:
            prefix = source.read(len(MAGIC) + 4)
            if prefix[:len(MAGIC)] != MAGIC or len(prefix) != len(MAGIC) + 4:
                raise ArchiveError("archive_header_invalid")
            length = struct.unpack(">I", prefix[-4:])[0]
            if not 0 < length <= 16384:
                raise ArchiveError("archive_header_invalid")
            encoded = source.read(length)
            header = json.loads(encoded)
            if header.get("format") != FORMAT or any(header.get(field) != descriptor[field] for field in (
                    "dataset_id", "kind", "shard", "created_at", "plaintext_bytes", "plaintext_sha256", "row_counts")):
                raise ArchiveError("archive_header_mismatch")
            _context(header["dataset_id"], header["kind"], header["shard"])
            limit = CATALOG_LIMIT if header["kind"] == "catalog" else SHARD_LIMIT
            if type(header["plaintext_bytes"]) is not int or not 0 < header["plaintext_bytes"] <= limit:
                raise ArchiveError("archive_plaintext_too_large")
            nonce = base64.b64decode(header["nonce"], validate=True)
            if len(nonce) != 12:
                raise ArchiveError("archive_header_invalid")
            start = source.tell()
            remaining = encrypted.stat().st_size - start - 16
            if remaining <= 0:
                raise ArchiveError("archive_truncated")
            source.seek(-16, 2)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, source.read(16))).decryptor()
            decryptor.authenticate_additional_data(prefix + encoded)
            source.seek(start)
            fd, name = tempfile.mkstemp(prefix="compressed-", dir=destination.parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as compressed:
                while remaining:
                    chunk = source.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ArchiveError("archive_truncated")
                    remaining -= len(chunk)
                    compressed.write(decryptor.update(chunk))
                compressed.write(decryptor.finalize())
        written = 0
        with gzip.open(temporary, "rb") as compressed, destination.open("xb") as target:
            created = True
            os.chmod(destination, 0o600)
            while chunk := compressed.read(min(CHUNK, header["plaintext_bytes"] - written + 1)):
                written += len(chunk)
                if written > header["plaintext_bytes"]:
                    raise ArchiveError("archive_decompression_limit")
                target.write(chunk)
        if written != header["plaintext_bytes"] or file_sha256(destination) != header["plaintext_sha256"]:
            raise ArchiveError("archive_plaintext_mismatch")
        info = validate_sqlite(destination, dataset_id=header["dataset_id"], kind=header["kind"],
                               shard=header["shard"], expected_counts=header["row_counts"])
        if info["created_at"] != header["created_at"]:
            raise ArchiveError("archive_metadata_mismatch")
        return info
    except ArchiveError:
        if created and destination.exists():
            destination.unlink()
        raise
    except (InvalidTag, ValueError, KeyError, TypeError, OSError, EOFError, zlib.error):
        if created and destination.exists():
            destination.unlink()
        raise ArchiveError("archive_authentication_or_format_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
