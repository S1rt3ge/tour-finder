"""Archive tests use only generated public-tour fixtures and fake HTTP."""
from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import gzip

import pytest
import requests

from tourfinder import archive_format as fmt
from tourfinder.archive_store import ArchiveStore, DEFAULT_MANIFEST_URL, validate_manifest, validate_url

KEY = bytes(range(32))
DATASET = "test-dataset-1"
CREATED = "2026-10-09T00:00:00Z"
RELEASE = "https://github.com/S1rt3ge/tour-finder/releases/download/test-archive/"
OFFER = dict(id=1, source="joinup", source_hotel_id="hotel-1", origin_id="RIX", origin_name="Riga",
             date_start="2026-10-20", date_end="2026-10-27", nights=7, board_code="AI", board_name="All inclusive",
             room_code="standard", room_name="Standard", room_placement="2AD", pax_adl=2, pax_chd=0,
             children_ages="", operator="joinup", link="https://example.test/hotel",
             first_seen_at=CREATED, last_seen_at=CREATED)
OBSERVATION = dict(snapshot_id=11, run_id=1, fetched_at=CREATED, price_cents=80000, currency="EUR",
                   is_hot=0, availability=None, stop_sale=None, operator_avg_price_cents=None)


def insert(conn, table, row):
    columns = ",".join(row)
    marks = ",".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table}({columns}) VALUES ({marks})", tuple(row.values()))


def make_catalog(path):
    conn = fmt.create_catalog(path, dataset_id=DATASET, created_at=CREATED)
    insert(conn, "hotels", dict(source="joinup", source_hotel_id="hotel-1", name="Fixture Hotel"))
    insert(conn, "offers", OFFER)
    insert(conn, "archive_offer_keys", dict(offer_id=1, offer_key=fmt.offer_key(OFFER)))
    insert(conn, "price_snapshots", {"id": 11, "offer_id": 1, **{k: v for k, v in OBSERVATION.items() if k != "snapshot_id"}})
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("archive-fixtures")
    key = fmt.offer_key(OFFER)
    objects = {}
    catalog = root / "catalog.sqlite"
    make_catalog(catalog)

    def seal(path, kind, shard=None):
        name = "catalog.tfarc" if kind == "catalog" else f"history-{shard}.tfarc"
        encrypted = root / name
        result = fmt.encrypt_sqlite(path, encrypted, key=KEY, dataset_id=DATASET, kind=kind, shard=shard)
        result["url"] = RELEASE + name
        objects[result["url"]] = encrypted.read_bytes()
        return result

    manifest = {"format": fmt.FORMAT, "dataset_id": DATASET, "generation": "generation-1",
                "created_at": CREATED, "catalog": seal(catalog, "catalog"), "history": {}}
    for shard in range(fmt.SHARDS):
        path = root / f"history-{shard}.sqlite"
        conn = fmt.create_history(path, dataset_id=DATASET, shard=shard, created_at=CREATED)
        if shard == fmt.history_shard(key):
            for observation in [OBSERVATION, OBSERVATION | {"snapshot_id": 12, "price_cents": 75000}]:
                insert(conn, "archive_history", {"offer_key": key,
                    "observation_key": fmt.observation_key(key, observation), **observation})
        conn.commit()
        conn.close()
        manifest["history"][str(shard)] = seal(path, "history", shard)
    raw = fmt.canonical_json(manifest)
    objects[RELEASE + "manifest.json"] = raw
    pointer = {"format": fmt.POINTER_FORMAT, "dataset_id": DATASET, "generation": "generation-1",
               "manifest_url": RELEASE + "manifest.json", "manifest_bytes": len(raw),
               "manifest_sha256": hashlib.sha256(raw).hexdigest()}
    objects[DEFAULT_MANIFEST_URL] = fmt.canonical_json(pointer)
    return {"manifest": manifest, "objects": objects, "key": key, "root": root}


class Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = headers or {"Content-Length": str(len(body))}

    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), 1024):
            yield self.body[i:i + 1024]

    def close(self):
        pass


class Session:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.calls = []

    def get(self, url, **kwargs):
        assert kwargs == {"stream": True, "allow_redirects": False, "timeout": (5, 30)}
        self.calls.append(url)
        value = self.objects[url]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(value)


def store(bundle, tmp_path, **changes):
    session = Session(bundle["objects"])
    result = ArchiveStore(manifest_url=DEFAULT_MANIFEST_URL, key=KEY,
                          cache_dir=tmp_path / "cache", session=session, **changes)
    return result, session


def test_stable_identity_ignores_numeric_ids_labels_and_sorted_child_order():
    value = OFFER | {"pax_chd": 2, "children_ages": "7,4"}
    assert fmt.offer_key(value) == fmt.offer_key(value | {"id": 999, "link": "changed", "room_name": "Renamed", "children_ages": "4,7"})
    for field, changed in [("source", "waavo"), ("room_code", "suite"), ("room_placement", "family"),
                           ("board_code", "HB"), ("date_start", "2026-10-21"), ("pax_adl", 3)]:
        assert fmt.offer_key(value) != fmt.offer_key(value | {field: changed})
    assert fmt.history_shard(fmt.offer_key(value)) == int(fmt.offer_key(value), 16) % 64


def test_observation_dedup_keeps_changed_observation_despite_reused_id():
    key = fmt.offer_key(OFFER)
    assert fmt.observation_key(key, OBSERVATION) == fmt.observation_key(key, OBSERVATION | {"snapshot_id": 999, "run_id": 10})
    for field, value in [("price_cents", 1), ("currency", "USD"), ("is_hot", 1), ("stop_sale", "closed"), ("availability", "full")]:
        assert fmt.observation_key(key, OBSERVATION) != fmt.observation_key(key, OBSERVATION | {field: value})


def test_unconfigured_reader_does_not_touch_database_or_network(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_ARCHIVE_KEY", raising=False)
    monkeypatch.delenv("TOUR_ARCHIVE_MANIFEST_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://never-connect.invalid/private")
    reader = ArchiveStore(cache_dir=tmp_path / "absent", session=Session({}))
    assert not reader.configured()
    with pytest.raises(fmt.ArchiveError, match="not_configured"):
        reader.manifest()
    assert not (tmp_path / "absent").exists()


def test_verified_catalog_is_readonly_and_lookup_uses_opaque_key(bundle, tmp_path):
    reader, session = store(bundle, tmp_path)
    with reader.load_catalog() as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM offers").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM offers")
    item = reader.lookup_offer("a_" + bundle["key"])
    assert item["offer_id"] == "a_" + bundle["key"]
    assert item["catalog_offer_id"] == 1 and item["hotel_name"] == "Fixture Hotel"
    assert item["archived"] and item["dataset_id"] == DATASET
    assert item["snapshots_count"] is None  # one latest row is not full history
    assert session.calls.count(bundle["manifest"]["catalog"]["url"]) == 1


def test_history_downloads_one_correct_shard_and_preserves_same_time_changes(bundle, tmp_path):
    reader, session = store(bundle, tmp_path)
    rows = reader.history(bundle["key"])
    assert [row["price_cents"] for row in rows] == [80000, 75000]
    assert len({row["observation_key"] for row in rows}) == 2
    assert all(row["offer_id"] == "a_" + bundle["key"] for row in rows)
    shard_urls = [url for url in session.calls if "/history-" in url]
    assert shard_urls == [bundle["manifest"]["history"][str(fmt.history_shard(bundle["key"]))]["url"]]


@pytest.mark.parametrize("url", [
    "http://github.com/S1rt3ge/tour-finder/releases/download/a/x", "https://evil.test/archive",
    "https://github.com/attacker/other/releases/download/a/x", "https://user:secret@github.com/S1rt3ge/tour-finder/releases/download/a/x",
    "https://github.com/S1rt3ge/tour-finder/releases/download/a/%2e%2e/x", "https://127.0.0.1/x",
    "https://raw.githubusercontent.com/S1rt3ge/tour-finder/main/.env",
])
def test_urls_cannot_escape_fixed_repo_or_use_local_services(url):
    with pytest.raises(fmt.ArchiveError, match="url_not_allowed"):
        validate_url(url, manifest=True)


def test_redirect_to_private_network_is_rejected_before_request(bundle, tmp_path):
    reader, session = store(bundle, tmp_path)
    session.objects[DEFAULT_MANIFEST_URL] = Response(status=302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
    with pytest.raises(fmt.ArchiveError, match="url_not_allowed"):
        reader.manifest()
    assert session.calls == [DEFAULT_MANIFEST_URL]


def test_github_asset_cdn_redirect_is_supported(bundle, tmp_path):
    reader, session = store(bundle, tmp_path)
    url = bundle["manifest"]["catalog"]["url"]
    cdn = "https://release-assets.githubusercontent.com/release/opaque?signature=public-download"
    session.objects[cdn] = session.objects[url]
    session.objects[url] = Response(status=302, headers={"Location": cdn})
    assert reader.lookup_offer(bundle["key"])["price_cents"] == 80000


@pytest.mark.parametrize("change", ["missing_shard", "wrong_dataset", "oversized_plaintext", "wrong_count_tables"])
def test_manifest_must_be_complete_bounded_and_one_dataset(bundle, change):
    manifest = deepcopy(bundle["manifest"])
    if change == "missing_shard":
        del manifest["history"]["63"]
    elif change == "wrong_dataset":
        manifest["catalog"]["dataset_id"] = "different"
    elif change == "oversized_plaintext":
        manifest["catalog"]["plaintext_bytes"] = fmt.CATALOG_LIMIT + 1
    else:
        manifest["catalog"]["row_counts"]["telegram_users"] = 0
    with pytest.raises(fmt.ArchiveError):
        validate_manifest(manifest)


def test_ciphertext_tampering_never_opens_sqlite_or_decompresses(bundle, tmp_path, monkeypatch):
    descriptor = deepcopy(bundle["manifest"]["catalog"])
    body = bytearray(bundle["objects"][descriptor["url"]])
    body[-17] ^= 1
    encrypted = tmp_path / "tampered.tfarc"
    encrypted.write_bytes(body)
    descriptor["sha256"] = hashlib.sha256(body).hexdigest()  # bypass outer digest: test GCM itself
    monkeypatch.setattr(fmt.gzip, "open", lambda *_a, **_k: pytest.fail("decompressed before authentication"))
    with pytest.raises(fmt.ArchiveError, match="authentication"):
        fmt.decrypt_sqlite(encrypted, tmp_path / "plain.sqlite", key=KEY, descriptor=descriptor)
    assert not (tmp_path / "plain.sqlite").exists()
    assert not list(tmp_path.glob("compressed-*"))


def test_wrong_key_and_truncated_download_fail_without_published_cache(bundle, tmp_path):
    reader, session = store(bundle, tmp_path)
    reader._key = b"x" * 32
    with pytest.raises(fmt.ArchiveError, match="authentication"):
        reader.load_catalog()
    assert not list((tmp_path / "cache").glob("*.sqlite"))
    reader._key = KEY
    url = bundle["manifest"]["catalog"]["url"]
    session.objects[url] = session.objects[url][:-1]
    with pytest.raises(fmt.ArchiveError, match="download_size_mismatch"):
        reader.load_catalog()
    assert not list((tmp_path / "cache").glob("stage-*"))


def test_private_tables_and_mismatched_identity_cannot_be_published(tmp_path):
    catalog = tmp_path / "catalog.sqlite"
    make_catalog(catalog)
    with sqlite3.connect(catalog) as conn:
        conn.execute("CREATE TABLE telegram_users(secret TEXT)")
    with pytest.raises(fmt.ArchiveError, match="schema_invalid"):
        fmt.encrypt_sqlite(catalog, tmp_path / "bad.tfarc", key=KEY, dataset_id=DATASET, kind="catalog")
    with sqlite3.connect(catalog) as conn:
        conn.execute("DROP TABLE telegram_users")
        conn.execute("UPDATE offers SET room_code='different-room'")
    with pytest.raises(fmt.ArchiveError, match="identity_mismatch"):
        fmt.encrypt_sqlite(catalog, tmp_path / "bad.tfarc", key=KEY, dataset_id=DATASET, kind="catalog")
    assert not (tmp_path / "bad.tfarc").exists()


def test_encrypt_refuses_existing_destination_without_removing_it(bundle, tmp_path):
    target = tmp_path / "existing.tfarc"
    target.write_bytes(b"preserve")
    with pytest.raises(fmt.ArchiveError, match="destination_exists"):
        fmt.encrypt_sqlite(bundle["root"] / "catalog.sqlite", target, key=KEY, dataset_id=DATASET, kind="catalog")
    assert target.read_bytes() == b"preserve"


def test_row_counts_are_checked_after_authentication(bundle, tmp_path):
    catalog = bundle["root"] / "catalog.sqlite"
    counts = deepcopy(bundle["manifest"]["catalog"]["row_counts"])
    counts["offers"] += 1
    with pytest.raises(fmt.ArchiveError, match="row_counts_mismatch"):
        fmt.validate_sqlite(catalog, dataset_id=DATASET, kind="catalog", expected_counts=counts)


def test_cache_reserves_staging_space_before_download(bundle, tmp_path):
    reader, session = store(bundle, tmp_path, temp_limit=100)
    with pytest.raises(fmt.ArchiveError, match="capacity_exceeded"):
        reader.load_catalog()
    assert bundle["manifest"]["catalog"]["url"] not in session.calls
    assert not list((tmp_path / "cache").glob("stage-*"))


def test_network_errors_do_not_expose_tokens_or_connection_text(bundle, tmp_path):
    reader, session = store(bundle, tmp_path)
    session.objects[DEFAULT_MANIFEST_URL] = requests.Timeout("secret-token@private.host")
    with pytest.raises(fmt.ArchiveError) as error:
        reader.manifest()
    assert str(error.value) == "archive_download_failed"


def test_key_encoding_is_explicit_and_manifest_cache_cannot_be_mutated(bundle, tmp_path):
    assert fmt.key_from_string(base64.b64encode(KEY).decode()) == KEY
    assert fmt.key_from_string(KEY.hex()) == KEY
    with pytest.raises(fmt.ArchiveError):
        fmt.key_from_string("short-secret")
    reader, session = store(bundle, tmp_path)
    value = reader.manifest()
    value["history"].clear()
    assert len(reader.manifest()["history"]) == 64
    assert session.calls.count(DEFAULT_MANIFEST_URL) == 1


def test_authenticated_compression_bomb_is_bounded_before_sqlite(tmp_path):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    header = {"format": fmt.FORMAT, "dataset_id": DATASET, "kind": "catalog", "shard": None,
              "created_at": CREATED, "row_counts": {}, "plaintext_bytes": 16,
              "plaintext_sha256": hashlib.sha256(b"x" * 16).hexdigest(),
              "nonce": base64.b64encode(b"n" * 12).decode()}
    encoded = fmt.canonical_json(header)
    aad = fmt.MAGIC + struct.pack(">I", len(encoded)) + encoded
    body = aad + AESGCM(KEY).encrypt(b"n" * 12, gzip.compress(b"x" * (2 * fmt.MIB)), aad)
    encrypted = tmp_path / "bomb.tfarc"
    encrypted.write_bytes(body)
    descriptor = {k: v for k, v in header.items() if k not in {"format", "nonce"}} | {
        "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    with pytest.raises(fmt.ArchiveError, match="decompression_limit"):
        fmt.decrypt_sqlite(encrypted, tmp_path / "out.sqlite", key=KEY, descriptor=descriptor)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bomb.tfarc"]


def test_cache_cannot_relabel_verified_content_as_a_different_dataset(bundle, tmp_path):
    reader, session = store(bundle, tmp_path)
    assert reader.lookup_offer(bundle["key"])
    forged = deepcopy(bundle["manifest"])
    forged["dataset_id"] = "different-dataset"
    for asset in [forged["catalog"], *forged["history"].values()]:
        asset["dataset_id"] = forged["dataset_id"]
    session.objects[DEFAULT_MANIFEST_URL] = fmt.canonical_json(forged)
    reader.manifest(force=True)
    with pytest.raises(fmt.ArchiveError, match="header_mismatch"):
        reader.lookup_offer(bundle["key"])


def test_open_catalog_is_pinned_until_close_then_can_be_evicted(bundle, tmp_path):
    catalog_bytes = bundle["manifest"]["catalog"]["plaintext_bytes"]
    shard = bundle["manifest"]["history"][str(fmt.history_shard(bundle["key"]))]
    reader, _ = store(bundle, tmp_path, cache_limit=catalog_bytes + shard["plaintext_bytes"] - 1)
    catalog = reader.load_catalog()
    try:
        with pytest.raises(fmt.ArchiveError, match="capacity_exceeded"):
            reader.history(bundle["key"])
        assert catalog.execute("SELECT count(*) FROM offers").fetchone()[0] == 1
    finally:
        catalog.close()
    assert len(reader.history(bundle["key"])) == 2
    assert sum(p.stat().st_size for p in (tmp_path / "cache").glob("*.sqlite")) <= reader._cache_limit


def test_exporter_copies_verified_shard_to_new_database_without_source_mutation(bundle, tmp_path):
    reader, _ = store(bundle, tmp_path)
    manifest = reader.manifest()
    with reader.load_history_shard(fmt.history_shard(bundle["key"]), manifest=manifest) as source:
        with sqlite3.connect(tmp_path / "next-generation.sqlite") as target:
            source.backup_to(target)
            assert target.execute("SELECT count(*) FROM archive_history").fetchone()[0] == 2
            target.execute("DELETE FROM archive_history")
        assert source.execute("SELECT count(*) FROM archive_history").fetchone()[0] == 2
    assert len(reader.history(bundle["key"])) == 2
