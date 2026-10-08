"""Export verified tour-only archives, then optionally prune exact saved rows.

Never imports app startup or opens the default database. Plaintext and deletion
plans live only in the runner's private temporary directory. GitHub publication
uses a separate credentialed API session; public downloads carry no token.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid

import psycopg
from psycopg.rows import dict_row
import requests

from . import archive_format as fmt
from .archive_store import (ArchiveStore, DEFAULT_MANIFEST_URL, MANIFEST_LIMIT,
                            validate_asset_capacity, validate_manifest)

REPOSITORY = "S1rt3ge/tour-finder"
API = f"https://api.github.com/repos/{REPOSITORY}"
BRANCH = "archive-index"
KEEP_DAYS = 14
TRIGGER_BYTES = 350 * fmt.MIB
TARGET_BYTES = 300 * fmt.MIB
CHUNK_ROWS = 1000
PROGRESS_ROWS = 250_000
PRUNE_BATCH_OFFERS = 250
PRUNE_BATCH_ROWS = 5_000
PRUNE_SINGLE_OFFER_ROWS = 10_000
RUN_BUDGET_SECONDS = 42 * 60

HOTEL_COLUMNS = tuple("source source_hotel_id name category country_id country_name city_name latitude longitude photo_url".split())
OFFER_COLUMNS = tuple("source source_hotel_id origin_id origin_name date_start date_end nights board_code board_name room_code room_name room_placement pax_adl pax_chd children_ages operator link first_seen_at last_seen_at".split())
REVIEW_COLUMNS = tuple("source source_hotel_id platform rating rating_scale reviews_count summary external_id url matched_name match_status fetched_at".split())
SNAPSHOT_COLUMNS = tuple("offer_id run_id fetched_at price_cents currency is_hot availability stop_sale operator_avg_price_cents".split())
STATE_COLUMNS = tuple("price_cents currency is_hot availability stop_sale operator_avg_price_cents".split())


def stamp(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(stage, **fields):
    print(json.dumps({"stage": stage, **fields}, sort_keys=True), flush=True)


def source_url():
    url = os.environ.get("DATABASE_URL", "").strip().replace("postgresql+psycopg://", "postgresql://", 1)
    if not url.startswith(("postgresql://", "postgres://")):
        raise fmt.ArchiveError("postgresql_database_url_required")
    return url


def connect_source(url):
    return psycopg.connect(url, autocommit=True, prepare_threshold=None,
                          connect_timeout=15, row_factory=dict_row)


@contextmanager
def source_snapshot(conn):
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL lock_timeout='3s'")
        conn.execute("SET LOCAL statement_timeout='120s'")
        conn.execute("SET LOCAL max_parallel_workers_per_gather=0")
        metadata = conn.execute("""SELECT pg_export_snapshot() AS snapshot,
            pg_database_size(current_database()) AS database_bytes,
            pg_total_relation_size('public.offers') AS offer_bytes,
            pg_total_relation_size('public.price_snapshots') AS snapshot_bytes""").fetchone()
        yield metadata


def stream(conn, sql):
    with conn.cursor(name="archive_" + uuid.uuid4().hex) as cursor:
        cursor.execute(sql)
        while batch := cursor.fetchmany(CHUNK_ROWS):
            yield from batch


def insert_sqlite(conn, table, row, *, conflict=None):
    # Only explicit format-owned tables/columns reach this helper.
    columns = tuple(row)
    names = ",".join(f'"{name}"' for name in columns)
    sql = f'INSERT INTO "{table}"({names}) VALUES ({",".join("?" for _ in columns)})'
    if conflict:
        updates = ",".join(f'"{name}"=excluded."{name}"' for name in columns if name not in conflict)
        sql += f' ON CONFLICT({",".join(conflict)}) DO UPDATE SET {updates}'
    conn.execute(sql, tuple(row[name] for name in columns))


def keep_snapshot_ids(rows, cutoff):
    """Keep recent data and both ends of every historical state plateau.

    Full state includes currency/stop-sale/availability, not just the price.
    Change boundaries preserve sustained baselines and the first low observation.
    """
    if not rows:
        return set()
    kept = {rows[0]["id"], rows[-1]["id"]}
    for index, row in enumerate(rows):
        if row["fetched_at"] >= cutoff:
            kept.add(row["id"])
        if index and any(row[name] != rows[index - 1][name] for name in STATE_COLUMNS):
            kept.update((rows[index - 1]["id"], row["id"]))
    return kept


def new_plan(path, dataset_id, generation, metadata):
    conn = sqlite3.connect(path)
    os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.executescript("""CREATE TABLE plan_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE offers(source_id INTEGER PRIMARY KEY,offer_key TEXT NOT NULL,
            catalog_id INTEGER NOT NULL,payload TEXT NOT NULL,expired INTEGER NOT NULL,
            snapshots INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE snapshots(source_id INTEGER PRIMARY KEY,offer_id INTEGER NOT NULL,
            observation_key TEXT NOT NULL,payload TEXT NOT NULL,eligible INTEGER NOT NULL);
        CREATE INDEX plan_snapshots_offer ON snapshots(offer_id);
        CREATE INDEX plan_snapshots_eligible ON snapshots(eligible,source_id);""")
    conn.executemany("INSERT INTO plan_metadata VALUES (?,?)", [(k, str(v)) for k, v in {
        "dataset_id": dataset_id, "generation": generation, **metadata}.items()])
    conn.commit()
    return conn


def copy_previous(store, destination, *, manifest, shard=None, created_at):
    previous = store.load_catalog(manifest=manifest) if shard is None else store.load_history_shard(shard, manifest=manifest)
    with previous as source:
        source.backup_to(destination)
    destination.execute("UPDATE archive_metadata SET value=? WHERE key='created_at'", (created_at,))
    destination.commit()


@dataclass
class Export:
    directory: Path
    catalog: Path
    histories: list[Path]
    plan: Path
    dataset_id: str
    generation: str
    created_at: str
    counts: dict


def prepare_projection(directory, *, dataset_id, created_at, previous=None, previous_manifest=None):
    """Download/verify the prior generation BEFORE opening the PG snapshot."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if previous and (previous_manifest is None or previous_manifest["dataset_id"] != dataset_id):
        raise fmt.ArchiveError("archive_previous_manifest_required")
    for shard in [None, *range(fmt.SHARDS)]:
        path = directory / ("catalog.sqlite" if shard is None else f"history-{shard:02}.sqlite")
        conn = (fmt.create_catalog(path, dataset_id=dataset_id, created_at=created_at) if shard is None else
                fmt.create_history(path, dataset_id=dataset_id, shard=shard, created_at=created_at))
        try:
            if previous:
                copy_previous(previous, conn, manifest=previous_manifest, shard=shard, created_at=created_at)
        finally:
            conn.close()


def export_projection(conn, directory, *, dataset_id, generation, metadata, previous=None, previous_manifest=None, now=None, prepared=False):
    now = now or datetime.now(timezone.utc)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    created = stamp(now)
    cutoff = stamp(now - timedelta(days=KEEP_DAYS))
    catalog_path = directory / "catalog.sqlite"
    if not prepared:
        prepare_projection(directory, dataset_id=dataset_id, created_at=created,
                           previous=previous, previous_manifest=previous_manifest)
    catalog = sqlite3.connect(catalog_path)
    plan_path = directory / "deletion-plan.sqlite"
    plan = new_plan(plan_path, dataset_id, generation, metadata)
    histories = [directory / f"history-{i:02}.sqlite" for i in range(fmt.SHARDS)]
    shards = []
    counts = {"hotels": 0, "offers": 0, "reviews": 0, "snapshots": 0, "eligible_snapshots": 0, "expired_offers": 0}
    try:
        for index, path in enumerate(histories):
            shard = sqlite3.connect(path)
            shard.execute("PRAGMA cache_size=-1024")
            shards.append(shard)
        for row in stream(conn, f'SELECT {",".join(HOTEL_COLUMNS)} FROM public.hotels'):
            insert_sqlite(catalog, "hotels", dict(row), conflict=("source", "source_hotel_id"))
            counts["hotels"] += 1
        next_id = catalog.execute("SELECT coalesce(max(id),0)+1 FROM offers").fetchone()[0]
        emit("export_progress", phase="offers", rows=0)
        for raw in stream(conn, f'SELECT id,{",".join(OFFER_COLUMNS)} FROM public.offers ORDER BY id'):
            row = dict(raw)
            original = row.pop("id")
            key = fmt.offer_key(row)
            found = catalog.execute("SELECT offer_id FROM archive_offer_keys WHERE offer_key=?", (key,)).fetchone()
            target = found[0] if found else next_id
            if not found:
                next_id += 1
                catalog.execute("INSERT INTO archive_offer_keys VALUES (?,?)", (target, key))
            old_seen = catalog.execute("SELECT last_seen_at FROM offers WHERE id=?", (target,)).fetchone()
            if old_seen is None or row["last_seen_at"] >= old_seen[0]:
                insert_sqlite(catalog, "offers", {"id": target, **row}, conflict=("id",))
            expired = int(row["date_start"] < now.date().isoformat())
            plan.execute("INSERT INTO offers(source_id,offer_key,catalog_id,payload,expired) VALUES (?,?,?,?,?)",
                         (original, key, target, fmt.canonical_json(row).decode(), expired))
            counts["offers"] += 1
            counts["expired_offers"] += expired
            if counts["offers"] % PROGRESS_ROWS == 0:
                emit("export_progress", phase="offers", rows=counts["offers"])
        emit("export_progress", phase="offers", rows=counts["offers"], complete=True)
        # Reviews use their natural hotel/platform identity, never recycled IDs.
        for raw in stream(conn, f'SELECT {",".join(REVIEW_COLUMNS)} FROM public.hotel_reviews ORDER BY id'):
            row = dict(raw)
            old = catalog.execute("SELECT id,fetched_at FROM hotel_reviews WHERE source=? AND source_hotel_id=? AND platform=? ORDER BY id DESC LIMIT 1",
                                  tuple(row[k] for k in ("source", "source_hotel_id", "platform"))).fetchone()
            if old is None or row["fetched_at"] >= old[1]:
                target = old[0] if old else catalog.execute("SELECT coalesce(max(id),0)+1 FROM hotel_reviews").fetchone()[0]
                insert_sqlite(catalog, "hotel_reviews", {"id": target, **row}, conflict=("id",))
            counts["reviews"] += 1

        def observations(offer_id, rows):
            entry = plan.execute("SELECT * FROM offers WHERE source_id=?", (offer_id,)).fetchone()
            if entry is None:
                raise fmt.ArchiveError("archive_orphan_snapshot")
            key, target = entry["offer_key"], entry["catalog_id"]
            kept = keep_snapshot_ids(rows, cutoff)
            shard = shards[fmt.history_shard(key)]
            for raw in rows:
                row = dict(raw)
                original = row.pop("id")
                row.pop("offer_id")
                fingerprint = fmt.observation_key(key, row)
                public = {"observation_key": fingerprint, "offer_key": key, "snapshot_id": original, **row}
                # Existing observations survive ID reuse. Same-state duplicate
                # polls at the same timestamp intentionally share a fingerprint.
                shard.execute(f'INSERT INTO archive_history({",".join(public)}) VALUES ({",".join("?" for _ in public)}) ON CONFLICT(observation_key) DO NOTHING', tuple(public.values()))
                eligible = int(original not in kept and raw["fetched_at"] < cutoff)
                plan.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)",
                             (original, offer_id, fingerprint, fmt.canonical_json(dict(raw)).decode(), eligible))
                counts["eligible_snapshots"] += eligible
                counts["snapshots"] += 1
                if counts["snapshots"] % PROGRESS_ROWS == 0:
                    emit("export_progress", phase="history", rows=counts["snapshots"])
            last = dict(rows[-1])
            last.pop("id")
            last["offer_id"] = target
            current = catalog.execute("SELECT fetched_at FROM price_snapshots WHERE offer_id=?", (target,)).fetchone()
            if current is None or last["fetched_at"] >= current[0]:
                insert_sqlite(catalog, "price_snapshots", {"id": target, **last}, conflict=("id",))
            plan.execute("UPDATE offers SET snapshots=? WHERE source_id=?", (len(rows), offer_id))

        buffered, offer_id = [], None
        emit("export_progress", phase="history", rows=0)
        for row in stream(conn, f'SELECT id,{",".join(SNAPSHOT_COLUMNS)} FROM public.price_snapshots ORDER BY offer_id,fetched_at,id'):
            if offer_id is not None and row["offer_id"] != offer_id:
                observations(offer_id, buffered)
                buffered = []
            offer_id = row["offer_id"]
            buffered.append(dict(row))
            if len(buffered) > 100_000:
                raise fmt.ArchiveError("archive_single_offer_history_too_large")
        if buffered:
            observations(offer_id, buffered)
        emit("export_progress", phase="history", rows=counts["snapshots"], complete=True)
        if plan.execute("SELECT 1 FROM offers WHERE snapshots=0 LIMIT 1").fetchone():
            raise fmt.ArchiveError("archive_source_offer_without_history")
        catalog.commit()
        plan.commit()
        for shard in shards:
            shard.commit()
    finally:
        catalog.close()
        plan.close()
        for shard in shards:
            shard.close()
    # Fail before publication/deletion if the full catalog or a shard is too big.
    emit("projection_size", catalog_bytes=catalog_path.stat().st_size,
         largest_history_bytes=max(path.stat().st_size for path in histories), **counts)
    for kind, path, shard in [("catalog", catalog_path, None)] + [("history", p, i) for i, p in enumerate(histories)]:
        fmt.validate_sqlite(path, dataset_id=dataset_id, kind=kind, shard=shard)
    return Export(directory, catalog_path, histories, plan_path, dataset_id, generation, created, counts)


@dataclass(frozen=True)
class VerifiedPublication:
    dataset_id: str
    generation: str
    manifest_sha256: str
    pointer_commit: str


class Publisher:
    def __init__(self, token, *, session=None, public_session=None):
        if not token:
            raise fmt.ArchiveError("github_token_required")
        self.api = session or requests.Session()
        self.public = public_session or requests.Session()
        self.token = token
        self.expected_head = None
        self.previous_pointer = None

    def request(self, method, path, *, payload=None, data=None, content_type="application/json", missing=False):
        # Every endpoint and host is owned by this module, never a manifest.
        url = API + path
        if path.startswith("UPLOAD:"):
            release_id, filename = path[7:].split("/", 1)
            if not release_id.isdecimal() or not all(c.isalnum() or c in "._-" for c in filename):
                raise fmt.ArchiveError("archive_upload_path_invalid")
            url = f"https://uploads.github.com/repos/{REPOSITORY}/releases/{release_id}/assets?name={filename}"
        response = None
        try:
            response = self.api.request(method, url, json=payload, data=data,
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                         "Content-Type": content_type, "X-GitHub-Api-Version": "2022-11-28"},
                timeout=(10, 120), allow_redirects=False)
            if response.status_code == 404 and missing:
                return None
            if not 200 <= response.status_code < 300:
                raise fmt.ArchiveError("archive_github_request_failed")
            return response.json()
        except (requests.RequestException, ValueError):
            raise fmt.ArchiveError("archive_github_request_failed") from None
        finally:
            if response is not None:
                response.close()

    def current_pointer(self, *, initialize=False):
        reference = self.request("GET", f"/git/ref/heads/{BRANCH}", missing=True)
        if reference is None:
            if initialize:
                self.expected_head, self.previous_pointer = None, None
                return None
            raise fmt.ArchiveError("archive_pointer_missing")
        head = reference["object"]["sha"]
        content = self.request("GET", f"/contents/latest.json?ref={head}")
        try:
            raw = base64.b64decode(content["content"], validate=False)
            if len(raw) > MANIFEST_LIMIT:
                raise ValueError()
            pointer = json.loads(raw)
            if pointer["format"] != fmt.POINTER_FORMAT or pointer["manifest_bytes"] > MANIFEST_LIMIT:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise fmt.ArchiveError("archive_pointer_invalid") from None
        if initialize:
            self.expected_head, self.previous_pointer = head, pointer
        return head, pointer

    def upload(self, release_id, name, path):
        with Path(path).open("rb") as source:
            result = self.request("POST", f"UPLOAD:{release_id}/{name}", data=source,
                                  content_type="application/octet-stream")
        expected = f"https://github.com/{REPOSITORY}/releases/download/"
        url = result.get("browser_download_url", "")
        if not url.startswith(expected):
            raise fmt.ArchiveError("archive_upload_url_invalid")
        return url

    def publish_pointer(self, pointer):
        current = self.request("GET", f"/git/ref/heads/{BRANCH}", missing=True)
        if (current["object"]["sha"] if current else None) != self.expected_head:
            raise fmt.ArchiveError("archive_pointer_concurrent_change")
        base_tree = None
        if self.expected_head:
            base_tree = self.request("GET", f"/git/commits/{self.expected_head}")["tree"]["sha"]
        payload = {"tree": [{"path": "latest.json", "mode": "100644", "type": "blob",
                              "content": fmt.canonical_json(pointer).decode()}]}
        if base_tree:
            payload["base_tree"] = base_tree
        tree = self.request("POST", "/git/trees", payload=payload)["sha"]
        commit = self.request("POST", "/git/commits", payload={
            "message": f"Archive {pointer['generation']} [skip ci]", "tree": tree,
            "parents": [self.expected_head] if self.expected_head else []})["sha"]
        if self.expected_head:
            # A sibling change is not a fast-forward, so GitHub rejects races.
            self.request("PATCH", f"/git/refs/heads/{BRANCH}", payload={"sha": commit, "force": False})
        else:
            self.request("POST", "/git/refs", payload={"ref": f"refs/heads/{BRANCH}", "sha": commit})
        head, readback = self.current_pointer()
        if head != commit or readback != pointer:
            raise fmt.ArchiveError("archive_pointer_readback_mismatch")
        return commit

    def verify_public_pointer(self, pointer):
        # No token in this session. Cache-Control helps but is not trusted: a
        # cached old pointer fails equality and never authorizes deletion.
        response = None
        try:
            response = self.public.get(DEFAULT_MANIFEST_URL, timeout=(5, 30), allow_redirects=False, stream=True,
                                       headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
            if response.status_code != 200:
                raise fmt.ArchiveError("archive_public_pointer_not_fresh")
            body = bytearray()
            for chunk in response.iter_content(8192):
                body.extend(chunk)
                if len(body) > MANIFEST_LIMIT:
                    raise fmt.ArchiveError("archive_public_pointer_not_fresh")
            if json.loads(body) != pointer:
                raise fmt.ArchiveError("archive_public_pointer_not_fresh")
        except (requests.RequestException, ValueError):
            raise fmt.ArchiveError("archive_public_pointer_not_fresh") from None
        finally:
            if response is not None:
                response.close()


def publish_verified(export, publisher, *, key):
    # Validate EVERY asset's reader capacity before creating a release or
    # uploading anything. The reader stages ciphertext + authenticated gzip +
    # plaintext together, so a compressed-byte check alone is insufficient.
    entries = [("catalog", export.catalog, None)] + [("history", path, i) for i, path in enumerate(export.histories)]
    prepared = []
    for kind, path, shard in entries:
        name = "catalog.tfarc" if kind == "catalog" else f"history-{shard:02}.tfarc"
        encrypted = export.directory / name
        descriptor = fmt.encrypt_sqlite(path, encrypted, key=key, dataset_id=export.dataset_id, kind=kind, shard=shard)
        if shard is None:
            emit("publication_preflight", catalog_plaintext_bytes=descriptor["plaintext_bytes"],
                 catalog_ciphertext_bytes=descriptor["bytes"],
                 catalog_staged_bytes=2 * descriptor["bytes"] + descriptor["plaintext_bytes"])
        validate_asset_capacity(descriptor)
        prepared.append((name, encrypted, descriptor, shard))
    emit("publication_preflight", assets=len(prepared), capacity_verified=True)
    release = publisher.request("POST", "/releases", payload={
        "tag_name": "archive-history-" + export.generation, "name": "Tour history " + export.generation,
        "body": "Encrypted tour-only read archive. No Telegram users or subscriptions.",
        "draft": False, "prerelease": True,
    })
    release_id = int(release["id"])
    manifest = {"format": fmt.FORMAT, "dataset_id": export.dataset_id,
                "generation": export.generation, "created_at": export.created_at, "history": {}}
    checker = ArchiveStore(manifest_url=DEFAULT_MANIFEST_URL, key=key, session=publisher.public,
                           cache_dir=export.directory / "verify-cache")
    for name, encrypted, descriptor, shard in prepared:
        descriptor["url"] = publisher.upload(release_id, name, encrypted)
        # Download every published ciphertext and verify SHA, GCM, exact schema,
        # identities, SQLite integrity and every table count BEFORE manifest.
        downloaded = export.directory / (name + ".download")
        plain = export.directory / (name + ".verified.sqlite")
        with downloaded.open("xb") as target:
            checker._download(descriptor["url"], target, limit=descriptor["bytes"], expected_size=descriptor["bytes"])
        fmt.decrypt_sqlite(downloaded, plain, key=key, descriptor=descriptor)
        downloaded.unlink()
        plain.unlink()
        if shard is None:
            manifest["catalog"] = descriptor
        else:
            manifest["history"][str(shard)] = descriptor
    validate_manifest(manifest)
    raw = fmt.canonical_json(manifest)
    manifest_path = export.directory / "manifest.json"
    manifest_path.write_bytes(raw)
    url = publisher.upload(release_id, "manifest.json", manifest_path)
    digest = hashlib.sha256(raw).hexdigest()
    downloaded_manifest = checker._json(url, expected_size=len(raw), expected_hash=digest)
    if downloaded_manifest != manifest:
        raise fmt.ArchiveError("archive_manifest_readback_mismatch")
    pointer = {"format": fmt.POINTER_FORMAT, "dataset_id": export.dataset_id, "generation": export.generation,
               "manifest_url": url, "manifest_sha256": digest, "manifest_bytes": len(raw)}
    commit = publisher.publish_pointer(pointer)
    publisher.verify_public_pointer(pointer)
    return VerifiedPublication(export.dataset_id, export.generation, digest, commit)


def _same_public_row(actual, expected, columns):
    return all(actual.get(name) == expected.get(name) for name in columns)


class _PruneDeadline(Exception):
    """Roll back a batch when its client-side deadline expires."""


def _require_prune_time(deadline):
    if time.monotonic() >= deadline:
        raise _PruneDeadline()


def _write_transaction(conn, deadline):
    _require_prune_time(deadline)
    conn.execute("SET TRANSACTION READ WRITE")
    conn.execute("SET LOCAL lock_timeout='3s'")
    milliseconds = max(1, min(30_000, int((deadline - time.monotonic()) * 1000)))
    conn.execute(f"SET LOCAL statement_timeout='{milliseconds}ms'")


def _prune_batches(plan, stats):
    """Stream the plan once; missing/protected offers never consume delete caps."""
    candidates = plan.execute("""SELECT o.* FROM offers o WHERE o.expired=1 OR EXISTS
        (SELECT 1 FROM snapshots s WHERE s.offer_id=o.source_id AND s.eligible=1)
        ORDER BY o.expired DESC,o.source_id""")
    batch, rows = [], 0
    for item in candidates:
        if item["snapshots"] > PRUNE_SINGLE_OFFER_ROWS:
            stats["oversized_offers"] += 1
            continue
        if batch and (len(batch) >= PRUNE_BATCH_OFFERS or rows + item["snapshots"] > PRUNE_BATCH_ROWS):
            yield batch
            batch, rows = [], 0
        batch.append(item)
        rows += item["snapshots"]
    if batch:
        yield batch


def _prune_batch(conn, plan, items, *, available_snapshots, available_offers, deadline):
    """One bounded transaction; return counts only after its commit succeeds.

    The table locks protect expired-offer deletion even without foreign keys.
    Every candidate's complete rowset is compared with its verified export;
    changed, newly polled, reused-ID, or alert-referenced offers are skipped.
    """
    expected = {}
    for item in items:
        if item["expired"] and available_offers <= 0:
            continue
        if item["expired"] and item["snapshots"] > available_snapshots:
            continue
        _require_prune_time(deadline)
        rows = {r["source_id"]: (json.loads(r["payload"]), r["eligible"], r["observation_key"])
                for r in plan.execute("SELECT * FROM snapshots WHERE offer_id=?", (item["source_id"],))}
        expected[item["source_id"]] = (item, json.loads(item["payload"]), rows)
    result = {"deleted_snapshots": 0, "deleted_offers": 0, "changed_or_protected": 0, "already_absent": 0}
    if not expected:
        return result
    ids = sorted(expected)
    with conn.transaction():
        _write_transaction(conn, deadline)
        actual = conn.execute(f'SELECT id,{",".join(OFFER_COLUMNS)} FROM public.offers WHERE id = ANY(%s) ORDER BY id FOR UPDATE',
                              (ids,)).fetchall()
        actual = {row["id"]: row for row in actual}
        valid = []
        for offer_id in ids:
            if offer_id not in actual:
                result["already_absent"] += 1
            elif not _same_public_row(actual[offer_id], expected[offer_id][1], OFFER_COLUMNS):
                result["changed_or_protected"] += 1
            else:
                valid.append(offer_id)
        expired = [offer_id for offer_id in valid if expected[offer_id][0]["expired"]]
        if expired:
            _require_prune_time(deadline)
            conn.execute("LOCK TABLE public.alerts IN SHARE MODE")
            conn.execute("LOCK TABLE public.price_snapshots IN SHARE ROW EXCLUSIVE MODE")
            protected = {row["offer_id"] for row in conn.execute(
                "SELECT DISTINCT offer_id FROM public.alerts WHERE offer_id = ANY(%s)", (expired,)).fetchall()}
            result["changed_or_protected"] += len(protected)
            valid = [offer_id for offer_id in valid if offer_id not in protected]
        if not valid:
            _require_prune_time(deadline)
            return result
        _require_prune_time(deadline)
        # One extra row detects growth without loading an unbounded rowset.
        expected_count = sum(len(expected[offer_id][2]) for offer_id in valid)
        current_rows = conn.execute(
            f'SELECT id,{",".join(SNAPSHOT_COLUMNS)} FROM public.price_snapshots WHERE offer_id = ANY(%s) ORDER BY offer_id,id LIMIT %s FOR UPDATE',
            (valid, expected_count + 1)).fetchall()
        if len(current_rows) > expected_count:
            result["changed_or_protected"] += len(valid)
            _require_prune_time(deadline)
            return result
        by_offer = {offer_id: [] for offer_id in valid}
        for row in current_rows:
            by_offer[row["offer_id"]].append(row)
        snapshot_ids, offer_ids = [], []
        for offer_id in valid:
            item, _, expected_rows = expected[offer_id]
            rows = by_offer[offer_id]
            if (len(rows) != len(expected_rows) or any(r["id"] not in expected_rows
                    or not _same_public_row(r, expected_rows[r["id"]][0], SNAPSHOT_COLUMNS)
                    or fmt.observation_key(item["offer_key"], dict(r)) != expected_rows[r["id"]][2] for r in rows)):
                result["changed_or_protected"] += 1
                continue
            remaining = available_snapshots - len(snapshot_ids)
            eligible = [key for key, value in expected_rows.items() if item["expired"] or value[1]]
            if item["expired"]:
                if len(offer_ids) >= available_offers or len(eligible) > remaining:
                    continue
                offer_ids.append(offer_id)
            snapshot_ids.extend(eligible[:remaining])
        if snapshot_ids:
            _require_prune_time(deadline)
            deleted = conn.execute("DELETE FROM public.price_snapshots WHERE id = ANY(%s)", (snapshot_ids,))
            if deleted.rowcount != len(snapshot_ids):
                raise fmt.ArchiveError("archive_delete_count_mismatch")
        if offer_ids:
            _require_prune_time(deadline)
            deleted = conn.execute("DELETE FROM public.offers WHERE id = ANY(%s)", (offer_ids,))
            if deleted.rowcount != len(offer_ids):
                raise fmt.ArchiveError("archive_delete_count_mismatch")
        _require_prune_time(deadline)
        result["deleted_snapshots"], result["deleted_offers"] = len(snapshot_ids), len(offer_ids)
    return result


def prune_verified(conn, export, receipt, *, max_snapshots=50_000, max_offers=5_000, budget_seconds=600):
    if (not isinstance(receipt, VerifiedPublication) or receipt.dataset_id != export.dataset_id
            or receipt.generation != export.generation or not fmt.HEX.fullmatch(receipt.manifest_sha256)):
        raise fmt.ArchiveError("archive_verified_publication_required")
    if (not 0 <= max_snapshots <= 3_000_000 or not 0 <= max_offers <= 300_000
            or not 0 <= budget_seconds <= 900):
        raise fmt.ArchiveError("archive_prune_limit_invalid")
    plan = fmt.open_readonly(export.plan)
    metadata = dict(plan.execute("SELECT key,value FROM plan_metadata"))
    if metadata["dataset_id"] != receipt.dataset_id or metadata["generation"] != receipt.generation:
        plan.close()
        raise fmt.ArchiveError("archive_deletion_plan_mismatch")
    before = int(metadata["database_bytes"])
    stats = {"deleted_snapshots": 0, "deleted_offers": 0, "changed_or_protected": 0,
             "already_absent": 0, "oversized_offers": 0, "batches": 0,
             "estimated_reusable_bytes": 0, "physical_reclaim_pending": True}
    remaining = max(0, before - TARGET_BYTES)
    snapshot_total = export.counts["snapshots"]
    offer_total = export.counts["offers"]
    per_snapshot = int(metadata["snapshot_bytes"]) / max(1, snapshot_total)
    per_offer = int(metadata["offer_bytes"]) / max(1, offer_total)
    deadline = time.monotonic() + budget_seconds
    try:
        if before < TRIGGER_BYTES:
            return stats | {"skipped": "below_trigger"}
        for items in _prune_batches(plan, stats):
            available = max_snapshots - stats["deleted_snapshots"]
            if (available <= 0 or time.monotonic() >= deadline
                    or stats["estimated_reusable_bytes"] >= remaining):
                break
            if stats["deleted_offers"] >= max_offers:
                items = [item for item in items if not item["expired"]]
                if not items:
                    continue
            started = time.monotonic()
            try:
                result = _prune_batch(conn, plan, items, available_snapshots=available,
                    available_offers=max_offers - stats["deleted_offers"], deadline=deadline)
            except _PruneDeadline:
                break
            for key, value in result.items():
                stats[key] += value
            stats["batches"] += 1
            stats["estimated_reusable_bytes"] += round(per_snapshot * result["deleted_snapshots"] + per_offer * result["deleted_offers"])
            emit("prune_progress", **stats, batch_seconds=round(time.monotonic() - started, 3))
        return stats | {"time_budget_reached": time.monotonic() >= deadline}
    finally:
        plan.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("export", "prune"), default="export")
    parser.add_argument("--maintenance", action="store_true", help="Explicit one-time larger prune budget; never scheduled")
    parser.add_argument("--max-snapshots", type=int)
    parser.add_argument("--max-offers", type=int)
    parser.add_argument("--prune-budget-seconds", type=int)
    args = parser.parse_args(argv)
    stage = "configuration"
    run_deadline = time.monotonic() + RUN_BUDGET_SECONDS
    try:
        if args.maintenance and (args.mode != "prune" or os.environ.get("GITHUB_EVENT_NAME") == "schedule"):
            raise fmt.ArchiveError("archive_maintenance_requires_manual_prune")
        snapshot_cap, offer_cap, time_cap = (3_000_000, 300_000, 900) if args.maintenance else (100_000, 20_000, 600)
        max_snapshots = args.max_snapshots if args.max_snapshots is not None else (3_000_000 if args.maintenance else 50_000)
        max_offers = args.max_offers if args.max_offers is not None else (300_000 if args.maintenance else 5_000)
        prune_budget = args.prune_budget_seconds if args.prune_budget_seconds is not None else time_cap
        if not 0 <= max_snapshots <= snapshot_cap or not 0 <= max_offers <= offer_cap or not 0 <= prune_budget <= time_cap:
            raise fmt.ArchiveError("archive_prune_limit_invalid")
        if args.mode == "prune" and os.environ.get("ARCHIVE_PRUNE_ENABLED") != "true":
            raise fmt.ArchiveError("archive_prune_not_enabled")
        if os.environ.get("TOUR_ARCHIVE_MANIFEST_URL") != DEFAULT_MANIFEST_URL:
            raise fmt.ArchiveError("archive_manifest_configuration_required")
        key = fmt.key_from_string(os.environ.get("APP_ARCHIVE_KEY", ""))
        url = source_url()
        if args.mode == "prune":
            # Avoid downloading an entire prior generation on a small DB. The
            # real export opens a separate consistent snapshot after preparation.
            stage = "size_check"
            with connect_source(url) as conn, source_snapshot(conn) as measured:
                if measured["database_bytes"] < TRIGGER_BYTES:
                    emit("complete", status="below_trigger", database_bytes=measured["database_bytes"])
                    return 0
        publisher = Publisher(os.environ.get("GITHUB_TOKEN", ""))
        stage = "previous_pointer"
        previous_pointer = publisher.current_pointer(initialize=True)
        previous, manifest = None, None
        dataset = "tour-finder-public-v1"
        with tempfile.TemporaryDirectory(prefix="tourfinder-archive-") as temporary:
            directory = Path(temporary)
            if previous_pointer:
                previous = ArchiveStore(manifest_url=DEFAULT_MANIFEST_URL, key=key, cache_dir=directory / "previous-cache")
                manifest = previous.manifest(force=True)
                expected = publisher.previous_pointer
                if (manifest["dataset_id"] != expected["dataset_id"] or manifest["generation"] != expected["generation"]
                        or hashlib.sha256(fmt.canonical_json(manifest)).hexdigest() != expected["manifest_sha256"]):
                    raise fmt.ArchiveError("archive_previous_pointer_not_fresh")
                dataset = manifest["dataset_id"]
            generation = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
            export_time = datetime.now(timezone.utc)
            stage = "prepare_previous"
            prepare_projection(directory / "generation", dataset_id=dataset, created_at=stamp(export_time),
                               previous=previous, previous_manifest=manifest)
            stage = "export"
            with connect_source(url) as conn, source_snapshot(conn) as metadata:
                if args.mode == "prune" and metadata["database_bytes"] < TRIGGER_BYTES:
                    emit("complete", status="below_trigger", database_bytes=metadata["database_bytes"])
                    return 0
                export = export_projection(conn, directory / "generation", dataset_id=dataset, generation=generation,
                                           metadata=metadata, now=export_time, prepared=True)
            emit("export", **export.counts, catalog_bytes=export.catalog.stat().st_size)
            stage = "publish_verify"
            receipt = publish_verified(export, publisher, key=key)
            emit("publication", verified=True, generation=generation)
            if args.mode == "prune":
                stage = "prune"
                with connect_source(url) as conn:
                    result = prune_verified(conn, export, receipt, max_snapshots=max_snapshots, max_offers=max_offers,
                        budget_seconds=min(prune_budget, max(0, run_deadline - time.monotonic())))
                emit("prune", **result)
            emit("complete", mode=args.mode, verified=True)
        return 0
    except Exception as exc:
        fields = {"error_type": type(exc).__name__}
        if isinstance(exc, fmt.ArchiveError):
            fields["code"] = str(exc)
        emit(stage, **fields)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
