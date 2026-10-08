"""Bounded, read-only access to verified public-tour archives on GitHub.

No DATABASE_URL, application startup, recovery key, or private Telegram table
is read. Network URLs come only from server configuration and validated
manifests, never from request parameters. All cache files are disposable.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests

from .archive_format import (
    ArchiveError, CATALOG_LIMIT, FORMAT, HEX, IDENTIFIER, MIB, POINTER_FORMAT,
    SCHEMAS, SHARD_LIMIT, SHARDS, decrypt_sqlite, file_sha256, history_shard,
    canonical_json, key_from_string, normalize_offer_key, open_readonly, validate_sqlite,
)

DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/S1rt3ge/tour-finder/archive-index/latest.json"
MANIFEST_LIMIT = 256 * 1024
CACHE_LIMIT = 256 * MIB
TEMP_LIMIT = 384 * MIB
_LOCK = threading.RLock()
_PINS = Counter()
_CDN_HOSTS = {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
_RELEASE_PATH = re.compile(r"^/S1rt3ge/tour-finder/releases/download/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def validate_url(value: str, *, manifest=False, redirect=False) -> str:
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.username or parts.password or parts.fragment
                or parts.port not in (None, 443)):
            raise ValueError()
        if redirect and parts.hostname in _CDN_HOSTS:
            return value
        if parts.query:
            raise ValueError()
        if manifest and parts.hostname == "raw.githubusercontent.com" and parts.path in {
            "/S1rt3ge/tour-finder/archive-index/latest.json",
            "/S1rt3ge/tour-finder/archive-index/manifest.json",
        }:
            return value
        if parts.hostname == "github.com" and _RELEASE_PATH.fullmatch(parts.path):
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    raise ArchiveError("archive_url_not_allowed")


def _integer(value, *, minimum=0, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ArchiveError("archive_manifest_invalid")
    return value


def validate_descriptor(value, *, dataset_id, kind, shard):
    if not isinstance(value, dict):
        raise ArchiveError("archive_manifest_invalid")
    limit = CATALOG_LIMIT if kind == "catalog" else SHARD_LIMIT
    if value.get("dataset_id") != dataset_id or value.get("kind") != kind or value.get("shard") != shard:
        raise ArchiveError("archive_manifest_identity_mismatch")
    for field in ("sha256", "plaintext_sha256"):
        if not isinstance(value.get(field), str) or not HEX.fullmatch(value[field]):
            raise ArchiveError("archive_manifest_invalid")
    _integer(value.get("plaintext_bytes"), minimum=1, maximum=limit)
    _integer(value.get("bytes"), minimum=1, maximum=limit + MIB)
    if not isinstance(value.get("created_at"), str) or len(value["created_at"]) > 40:
        raise ArchiveError("archive_manifest_invalid")
    counts = value.get("row_counts")
    if not isinstance(counts, dict) or set(counts) != set(SCHEMAS[kind]):
        raise ArchiveError("archive_manifest_invalid")
    for number in counts.values():
        _integer(number, maximum=100_000_000)
    validate_url(value.get("url"))


def validate_manifest(value):
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise ArchiveError("archive_manifest_invalid")
    for field in ("dataset_id", "generation"):
        if not isinstance(value.get(field), str) or not IDENTIFIER.fullmatch(value[field]):
            raise ArchiveError("archive_manifest_invalid")
    if not isinstance(value.get("created_at"), str) or len(value["created_at"]) > 40:
        raise ArchiveError("archive_manifest_invalid")
    validate_descriptor(value.get("catalog"), dataset_id=value["dataset_id"], kind="catalog", shard=None)
    history = value.get("history")
    if not isinstance(history, dict) or set(history) != {str(i) for i in range(SHARDS)}:
        raise ArchiveError("archive_manifest_incomplete")
    for i in range(SHARDS):
        validate_descriptor(history[str(i)], dataset_id=value["dataset_id"], kind="history", shard=i)
    return value


class ArchiveConnection:
    """Context-managed sqlite3-ish connection; closing releases the cache pin."""

    def __init__(self, path, manifest):
        self._path = path
        self.manifest = deepcopy(manifest)
        self._conn = open_readonly(path)
        self._closed = False

    def execute(self, query, params=None):
        deadline = time.monotonic() + 20
        self._conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        return self._conn.execute(query, params or {})

    def close(self):
        if not self._closed:
            self._conn.close()
            with _LOCK:
                _PINS[str(self._path)] -= 1
            self._closed = True

    def backup_to(self, destination):
        """Copy a verified projection into an explicitly supplied SQLite DB.

        This exporter hook never changes the cached read-only source. The
        destination must have no active write transaction.
        """
        if not isinstance(destination, sqlite3.Connection) or destination.in_transaction:
            raise ArchiveError("archive_backup_destination_invalid")
        database = destination.execute("PRAGMA database_list").fetchone()[2]
        if database and Path(database).resolve() == self._path.resolve():
            raise ArchiveError("archive_backup_destination_invalid")
        self._conn.backup(destination)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ArchiveStore:
    def __init__(self, *, manifest_url=None, key=None, cache_dir=None, session=None,
                 manifest_ttl=60, cache_limit=CACHE_LIMIT, temp_limit=TEMP_LIMIT):
        self._url = manifest_url if manifest_url is not None else os.environ.get("TOUR_ARCHIVE_MANIFEST_URL", "")
        raw_key = key if key is not None else os.environ.get("APP_ARCHIVE_KEY", "")
        self._key = key_from_string(raw_key) if isinstance(raw_key, str) and raw_key else raw_key or None
        if self._key is not None and (not isinstance(self._key, bytes) or len(self._key) != 32):
            raise ArchiveError("archive_key_invalid")
        if self._url:
            validate_url(self._url, manifest=True)
        self._dir = Path(cache_dir) if cache_dir is not None else Path(tempfile.gettempdir()) / "tourfinder-read-archive-v1"
        self._session = session or requests.Session()
        self._ttl = max(0, min(manifest_ttl, 300))
        self._cache_limit = min(cache_limit, CACHE_LIMIT)
        self._temp_limit = min(temp_limit, TEMP_LIMIT)
        self._manifest = None
        self._manifest_at = 0
        self._verified = {}

    def configured(self):
        return bool(self._url and self._key)

    def _download(self, url, output, *, limit, expected_size=None):
        url = validate_url(url, manifest=True)
        response = None
        deadline = time.monotonic() + 45
        try:
            for attempt in range(5):
                if time.monotonic() >= deadline:
                    raise ArchiveError("archive_download_timeout")
                response = self._session.get(url, stream=True, allow_redirects=False, timeout=(5, 30))
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = urljoin(url, response.headers.get("Location", ""))
                    response.close()
                    url = validate_url(target, manifest=True, redirect=True)
                    continue
                if response.status_code != 200:
                    raise ArchiveError("archive_download_failed")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > limit):
                    raise ArchiveError("archive_download_too_large")
                total = 0
                for chunk in response.iter_content(chunk_size=MIB):
                    if time.monotonic() >= deadline:
                        raise ArchiveError("archive_download_timeout")
                    total += len(chunk)
                    if total > limit:
                        raise ArchiveError("archive_download_too_large")
                    output.write(chunk)
                if expected_size is not None and total != expected_size:
                    raise ArchiveError("archive_download_size_mismatch")
                return
            raise ArchiveError("archive_redirect_limit")
        except requests.RequestException:
            raise ArchiveError("archive_download_failed") from None
        finally:
            if response is not None:
                response.close()

    def _json(self, url, *, expected_size=None, expected_hash=None):
        import io
        target = io.BytesIO()
        self._download(url, target, limit=MANIFEST_LIMIT, expected_size=expected_size)
        raw = target.getvalue()
        if expected_hash is not None and hashlib.sha256(raw).hexdigest() != expected_hash:
            raise ArchiveError("archive_manifest_hash_mismatch")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ArchiveError("archive_manifest_invalid") from None

    def manifest(self, *, force=False):
        if not self.configured():
            raise ArchiveError("archive_not_configured")
        with _LOCK:
            if not force and self._manifest and time.monotonic() - self._manifest_at < self._ttl:
                return deepcopy(self._manifest)
            value = self._json(self._url)
            if isinstance(value, dict) and value.get("format") == POINTER_FORMAT:
                size = _integer(value.get("manifest_bytes"), minimum=1, maximum=MANIFEST_LIMIT)
                digest = value.get("manifest_sha256")
                if not isinstance(digest, str) or not HEX.fullmatch(digest):
                    raise ArchiveError("archive_manifest_invalid")
                complete = self._json(value.get("manifest_url"), expected_size=size, expected_hash=digest)
                if not isinstance(complete, dict) or any(complete.get(k) != value.get(k) for k in ("dataset_id", "generation")):
                    raise ArchiveError("archive_manifest_identity_mismatch")
                value = complete
            validate_manifest(value)
            self._manifest, self._manifest_at = value, time.monotonic()
            return deepcopy(value)

    def _usage(self):
        return sum(p.stat().st_size for p in self._dir.rglob("*") if p.is_file() and not p.is_symlink())

    def _reserve(self, *, staged_bytes, plaintext_bytes):
        files = [p for p in self._dir.glob("*.sqlite") if HEX.fullmatch(p.stem) and not p.is_symlink()]
        files.sort(key=lambda p: p.stat().st_mtime)
        cached = sum(p.stat().st_size for p in files)
        total = self._usage()
        for path in files:
            if cached + plaintext_bytes <= self._cache_limit and total + staged_bytes <= self._temp_limit:
                break
            if _PINS[str(path)] > 0:
                continue
            size = path.stat().st_size
            path.unlink()
            self._verified.pop(path.name, None)
            cached -= size
            total -= size
        if cached + plaintext_bytes > self._cache_limit or total + staged_bytes > self._temp_limit:
            raise ArchiveError("archive_cache_capacity_exceeded")

    def _asset(self, descriptor):
        # Caller holds _LOCK until the returned file is pinned/opened. The
        # reservation includes ciphertext, authenticated gzip and SQLite output.
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._dir / (descriptor["sha256"] + ".sqlite")
        descriptor_hash = hashlib.sha256(canonical_json(descriptor)).hexdigest()
        if path.is_symlink():
            raise ArchiveError("archive_cache_path_invalid")
        if path.exists() and self._verified.get(path.name) == descriptor_hash:
            os.utime(path, None)
            return path
        if path.exists():
            try:
                if path.stat().st_size != descriptor["plaintext_bytes"] or file_sha256(path) != descriptor["plaintext_sha256"]:
                    raise ArchiveError("archive_cached_content_mismatch")
                info = validate_sqlite(path, dataset_id=descriptor["dataset_id"], kind=descriptor["kind"],
                                       shard=descriptor["shard"], expected_counts=descriptor["row_counts"])
                if info["created_at"] != descriptor["created_at"]:
                    raise ArchiveError("archive_metadata_mismatch")
                self._verified[path.name] = descriptor_hash
                return path
            except ArchiveError:
                if _PINS[str(path)]:
                    raise
                path.unlink()
        self._reserve(staged_bytes=2 * descriptor["bytes"] + descriptor["plaintext_bytes"],
                      plaintext_bytes=descriptor["plaintext_bytes"])
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=self._dir))
        try:
            encrypted, plain = stage / "asset.encrypted", stage / "asset.sqlite"
            with encrypted.open("xb") as output:
                self._download(descriptor["url"], output, limit=descriptor["bytes"], expected_size=descriptor["bytes"])
            decrypt_sqlite(encrypted, plain, key=self._key, descriptor=descriptor)
            plain.replace(path)
            self._verified[path.name] = descriptor_hash
            return path
        finally:
            # stage is exactly the private mkdtemp child created above.
            if stage.parent.resolve() != self._dir.resolve():
                raise ArchiveError("archive_cache_path_invalid")
            shutil.rmtree(stage)

    def _open(self, descriptor, manifest):
        with _LOCK:
            path = self._asset(descriptor)
            connection = ArchiveConnection(path, manifest)
            _PINS[str(path)] += 1
            return connection

    def load_catalog(self, *, manifest=None):
        """Return a context manager/closeable read-only SQLite connection."""
        manifest = self.manifest() if manifest is None else validate_manifest(deepcopy(manifest))
        return self._open(manifest["catalog"], manifest)

    def load_history_shard(self, shard, *, manifest=None):
        """Exporter hook; pass its initial manifest to pin one full generation."""
        if type(shard) is not int or not 0 <= shard < SHARDS:
            raise ArchiveError("archive_shard_invalid")
        manifest = self.manifest() if manifest is None else validate_manifest(deepcopy(manifest))
        return self._open(manifest["history"][str(shard)], manifest)

    def lookup_offer(self, key):
        key = normalize_offer_key(key)
        with self.load_catalog() as conn:
            row = conn.execute("""SELECT o.*,h.name AS hotel_name,h.category,h.country_name,
                h.city_name,h.photo_url,p.price_cents,p.currency,p.is_hot,p.fetched_at,
                p.availability,p.stop_sale,p.operator_avg_price_cents,
                r.platform AS review_platform,r.rating AS review_rating,r.rating_scale AS review_scale,
                r.reviews_count AS review_count,r.url AS review_url,r.match_status AS review_match_status
                FROM archive_offer_keys k JOIN offers o ON o.id=k.offer_id
                JOIN hotels h ON h.source=o.source AND h.source_hotel_id=o.source_hotel_id
                JOIN price_snapshots p ON p.offer_id=o.id
                LEFT JOIN hotel_reviews r ON r.id=(SELECT x.id FROM hotel_reviews x
                    WHERE x.source=o.source AND x.source_hotel_id=o.source_hotel_id
                    AND x.rating IS NOT NULL AND x.match_status='ok'
                    ORDER BY (x.reviews_count IS NULL),x.reviews_count DESC,x.id DESC LIMIT 1)
                WHERE k.offer_key=:key""", {"key": key}).fetchone()
            if row is None:
                return None
            result = dict(row)
            result.update(catalog_offer_id=result["id"], id="a_" + key, offer_id="a_" + key,
                          offer_key=key, data_source="archive", archived=True,
                          archive_as_of=conn.manifest["catalog"]["created_at"], dataset_id=conn.manifest["dataset_id"],
                          snapshots_count=None, avg_seen_cents=None, min_seen_cents=None, max_seen_cents=None)
            return result

    def history(self, key):
        key = normalize_offer_key(key)
        manifest = self.manifest()
        with self.load_history_shard(history_shard(key), manifest=manifest) as conn:
            rows = conn.execute("""SELECT * FROM archive_history WHERE offer_key=:key
                ORDER BY fetched_at,snapshot_id,observation_key LIMIT 10001""", {"key": key}).fetchall()
        if len(rows) > 10000:
            raise ArchiveError("archive_history_limit_exceeded")
        return [dict(row) | {"id": row["snapshot_id"], "offer_id": "a_" + key,
                            "dataset_id": manifest["dataset_id"], "data_source": "archive"} for row in rows]
