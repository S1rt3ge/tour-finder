"""Generated public fixtures; no external database, GitHub or default DB writes."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import base64
import json
from pathlib import Path
import sqlite3

import pytest

from tourfinder import archive_format as fmt
from tourfinder import archive_rotation as rotation
from tourfinder.archive_store import ArchiveStore, DEFAULT_MANIFEST_URL

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
KEY = bytes(range(32))
DATASET = "fixture-tours"
META = {"snapshot": "fixture-snapshot", "database_bytes": 400 * fmt.MIB,
        "offer_bytes": fmt.MIB, "snapshot_bytes": 300 * fmt.MIB}
HOTEL = dict.fromkeys(rotation.HOTEL_COLUMNS)
HOTEL.update(source="joinup", source_hotel_id="hotel-1", name="Fixture hotel")
OFFER = dict(id=41, source="joinup", source_hotel_id="hotel-1", origin_id="RIX", origin_name="Riga",
             date_start="2026-10-20", date_end="2026-10-27", nights=7, board_code="AI", board_name="All inclusive",
             room_code="standard", room_name="Standard", room_placement="2AD", pax_adl=2, pax_chd=0,
             children_ages="", operator="joinup", link="https://example.test/hotel",
             first_seen_at="2026-08-01T00:00:00Z", last_seen_at="2026-10-09T00:00:00Z")


def observation(i, **changes):
    return dict(id=i, offer_id=41, run_id=1, fetched_at=f"2026-09-{i:02}T00:00:00Z",
                price_cents=100000, currency="EUR", is_hot=0, availability=None,
                stop_sale=None, operator_avg_price_cents=None) | changes


class Result:
    def __init__(self, rows=(), rowcount=0):
        self.rows, self.rowcount = list(rows), rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Source:
    """PG-shaped source for streaming, read-only transaction and prune races."""
    def __init__(self, *, offer=None, observations=None, empty=False):
        self.tables = {"hotels": [dict(HOTEL)], "offers": [dict(offer or OFFER)],
                       "hotel_reviews": [], "price_snapshots": observations or [observation(i) for i in range(1, 6)]}
        if empty:
            self.tables = {key: [] for key in self.tables}
        self.sql, self.alerts, self.rollbacks = [], False, 0

    @contextmanager
    def transaction(self):
        original = deepcopy(self.tables)
        try:
            yield
        except Exception:
            self.tables = original
            self.rollbacks += 1
            raise

    def cursor(self, name):
        source = self

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql):
                source.sql.append(sql)
                name = sql.split("FROM public.", 1)[1].split()[0]
                self.rows = deepcopy(source.tables[name])
            def fetchmany(self, size):
                rows, self.rows = self.rows[:size], self.rows[size:]
                return rows
        return Cursor()

    def execute(self, sql, params=()):
        self.sql.append(sql)
        if sql.startswith(("SET", "LOCK")):
            return Result()
        if "pg_export_snapshot" in sql:
            return Result([META])
        if "FROM public.alerts" in sql:
            return Result([{"protected": 1}] if self.alerts else [])
        if sql.startswith("SELECT") and "FROM public.offers" in sql:
            return Result([r for r in self.tables["offers"] if r["id"] == params[0]])
        if sql.startswith("SELECT") and "FROM public.price_snapshots" in sql:
            return Result([r for r in self.tables["price_snapshots"] if r["offer_id"] == params[0]])
        if sql.startswith("DELETE FROM public.price_snapshots WHERE id = ANY"):
            ids = params[0]
            before = len(self.tables["price_snapshots"])
            self.tables["price_snapshots"] = [r for r in self.tables["price_snapshots"] if r["id"] not in ids]
            return Result(rowcount=before - len(self.tables["price_snapshots"]))
        if sql.startswith("DELETE FROM public.offers"):
            before = len(self.tables["offers"])
            self.tables["offers"] = [r for r in self.tables["offers"] if r["id"] != params[0]]
            return Result(rowcount=before - len(self.tables["offers"]))
        raise AssertionError(sql)


def exported(tmp_path, source=None, **kwargs):
    return rotation.export_projection(source or Source(), tmp_path, dataset_id=DATASET,
        generation=kwargs.pop("generation", "generation-1"), metadata=META, now=NOW, **kwargs)


def receipt(export):
    return rotation.VerifiedPublication(export.dataset_id, export.generation, "f" * 64, "c" * 40)


class Previous:
    """Validated prior local generation, exercising SQLite backup integration."""
    def __init__(self, export):
        self.export, self.manifests = export, []

    def open(self, path, manifest):
        self.manifests.append(manifest)
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def backup_to(self, destination):
                with sqlite3.connect(path) as source:
                    source.backup(destination)
        return Connection()

    def load_catalog(self, *, manifest): return self.open(self.export.catalog, manifest)
    def load_history_shard(self, shard, *, manifest): return self.open(self.export.histories[shard], manifest)


def rows(path, sql):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql).fetchall()


def test_source_snapshot_is_readonly_before_first_query():
    source = Source()
    with rotation.source_snapshot(source) as metadata:
        assert metadata["snapshot"] == "fixture-snapshot"
    assert "REPEATABLE READ READ ONLY" in source.sql[0]
    assert source.sql[-1].startswith("SELECT")
    assert any("statement_timeout" in sql for sql in source.sql)


def test_export_projects_only_tours_and_preserves_history_identity(tmp_path):
    source = Source()
    export = exported(tmp_path, source)
    assert export.counts["snapshots"] == 5
    assert rows(export.catalog, "SELECT id FROM offers") == [(1,)]
    assert rows(export.catalog, "SELECT price_cents FROM price_snapshots") == [(100000,)]
    key = fmt.offer_key(OFFER)
    history = rows(export.histories[fmt.history_shard(key)], "SELECT snapshot_id FROM archive_history ORDER BY snapshot_id")
    assert history == [(1,), (2,), (3,), (4,), (5,)]
    assert all("telegram" not in sql and "subscriptions" not in sql and "alerts" not in sql for sql in source.sql)
    assert rows(export.plan, "SELECT value FROM plan_metadata WHERE key='snapshot'") == [("fixture-snapshot",)]
    assert rows(export.plan, "SELECT source_id FROM snapshots WHERE eligible=1 ORDER BY source_id") == [(2,), (3,), (4,)]


def test_empty_live_database_preserves_catalog_and_all_prior_history(tmp_path):
    first = exported(tmp_path / "first")
    previous, manifest = Previous(first), {"dataset_id": DATASET, "generation": "generation-1"}
    second = exported(tmp_path / "second", Source(empty=True), generation="generation-2",
                      previous=previous, previous_manifest=manifest)
    assert second.counts["offers"] == 0
    assert rows(second.catalog, "SELECT id FROM offers") == [(1,)]
    key = fmt.offer_key(OFFER)
    assert rows(second.histories[fmt.history_shard(key)], "SELECT count(*) FROM archive_history") == [(5,)]
    assert len(previous.manifests) == 65 and all(value is manifest for value in previous.manifests)


def test_recycled_numeric_ids_do_not_overwrite_old_tour_or_observation(tmp_path):
    first = exported(tmp_path / "first")
    changed = OFFER | {"room_code": "suite"}
    source = Source(offer=changed, observations=[observation(1, price_cents=80000)])
    second = exported(tmp_path / "second", source, generation="generation-2",
                      previous=Previous(first), previous_manifest={"dataset_id": DATASET})
    assert rows(second.catalog, "SELECT id,room_code FROM offers ORDER BY id") == [(1, "standard"), (2, "suite")]
    oldkey, newkey = fmt.offer_key(OFFER), fmt.offer_key(changed)
    with sqlite3.connect(second.histories[fmt.history_shard(oldkey)]) as conn:
        assert conn.execute("SELECT count(*) FROM archive_history WHERE offer_key=?", (oldkey,)).fetchone()[0] == 5
    with sqlite3.connect(second.histories[fmt.history_shard(newkey)]) as conn:
        assert conn.execute("SELECT price_cents FROM archive_history WHERE offer_key=?", (newkey,)).fetchone()[0] == 80000


def test_same_offer_recycled_observation_id_preserves_both_price_states(tmp_path):
    first = exported(tmp_path / "first", Source(observations=[observation(1)]))
    second = exported(tmp_path / "second", Source(observations=[observation(1, price_cents=80000)]),
                      generation="generation-2", previous=Previous(first), previous_manifest={"dataset_id": DATASET})
    assert rows(second.catalog, "SELECT id FROM offers") == [(1,)]
    assert rows(second.catalog, "SELECT price_cents FROM price_snapshots") == [(80000,)]
    key = fmt.offer_key(OFFER)
    assert rows(second.histories[fmt.history_shard(key)], "SELECT price_cents FROM archive_history ORDER BY price_cents") == [(80000,), (100000,)]


@pytest.mark.parametrize("change", ["orphan", "missing_history"])
def test_unrepresented_source_data_fails_closed(tmp_path, change):
    source = Source()
    if change == "orphan": source.tables["price_snapshots"][0]["offer_id"] = 999
    else: source.tables["price_snapshots"] = []
    with pytest.raises(fmt.ArchiveError, match="orphan_snapshot|without_history"):
        exported(tmp_path, source)
    assert not any(sql.startswith("DELETE") for sql in source.sql)


def test_retention_keeps_both_change_boundaries_currency_stop_sale_and_recent():
    value = [observation(i) for i in range(1, 10)]
    value[3]["stop_sale"] = "closed"
    value[6]["currency"] = "USD"
    value[8]["fetched_at"] = "2026-10-09T00:00:00Z"
    kept = rotation.keep_snapshot_ids(value, "2026-09-25T00:00:00Z")
    assert kept == {1, 3, 4, 5, 6, 7, 8, 9}


@pytest.mark.parametrize("change", ["offer", "snapshot", "new_poll", "alert"])
def test_pruning_refuses_changed_rows_new_observations_and_alerts(tmp_path, change):
    source = Source(offer=OFFER | {"date_start": "2026-09-01"})
    export = exported(tmp_path, source)
    if change == "offer": source.tables["offers"][0]["last_seen_at"] = "2026-10-10T00:00:00Z"
    if change == "snapshot": source.tables["price_snapshots"][2]["stop_sale"] = "closed"
    if change == "new_poll": source.tables["price_snapshots"].append(observation(6))
    if change == "alert": source.alerts = True
    result = rotation.prune_verified(source, export, receipt(export))
    assert result["deleted_snapshots"] == result["deleted_offers"] == 0
    assert result["changed_or_protected"] == 1
    assert not any(sql.startswith("DELETE") for sql in source.sql)


def test_verified_pruning_batches_exact_ids_and_keeps_future_baseline(tmp_path):
    source = Source()
    export = exported(tmp_path, source)
    result = rotation.prune_verified(source, export, receipt(export))
    assert result["deleted_snapshots"] == 3 and result["deleted_offers"] == 0
    assert [r["id"] for r in source.tables["price_snapshots"]] == [1, 5]
    assert len(source.tables["offers"]) == 1
    deletes = [sql for sql in source.sql if sql.startswith("DELETE")]
    assert deletes == ["DELETE FROM public.price_snapshots WHERE id = ANY(%s)"]
    assert not any(word in " ".join(source.sql) for word in ("TRUNCATE", "VACUUM", "setval"))


def test_partial_delete_count_failure_rolls_back_all_changes(tmp_path):
    source = Source()
    export = exported(tmp_path, source)
    execute = source.execute
    def inconsistent(sql, params=()):
        result = execute(sql, params)
        if sql.startswith("DELETE"): result.rowcount = 0
        return result
    source.execute = inconsistent
    with pytest.raises(fmt.ArchiveError, match="delete_count_mismatch"):
        rotation.prune_verified(source, export, receipt(export))
    assert len(source.tables["price_snapshots"]) == 5 and source.rollbacks == 1


def test_zero_time_budget_never_starts_write_transaction(tmp_path):
    source = Source()
    export = exported(tmp_path, source)
    result = rotation.prune_verified(source, export, receipt(export), budget_seconds=0)
    assert result["time_budget_reached"] and result["deleted_snapshots"] == 0
    assert not any("READ WRITE" in sql for sql in source.sql)


def test_expired_offer_removal_obeys_snapshot_limit_and_locks_concurrent_writers(tmp_path):
    source = Source(offer=OFFER | {"date_start": "2026-09-01"})
    export = exported(tmp_path, source)
    result = rotation.prune_verified(source, export, receipt(export), max_snapshots=4)
    assert result["deleted_offers"] == result["deleted_snapshots"] == 0
    result = rotation.prune_verified(source, export, receipt(export), max_snapshots=5)
    assert result["deleted_offers"] == 1 and result["deleted_snapshots"] == 5
    assert source.tables["offers"] == source.tables["price_snapshots"] == []
    assert "LOCK TABLE public.alerts IN SHARE MODE" in source.sql
    assert "LOCK TABLE public.price_snapshots IN SHARE ROW EXCLUSIVE MODE" in source.sql


def test_no_pruning_without_matching_verified_publication(tmp_path):
    source, export = Source(), exported(tmp_path)
    for invalid in [None, receipt(export).__class__(DATASET, "other", "f" * 64, "c" * 40)]:
        with pytest.raises(fmt.ArchiveError, match="verified_publication_required"):
            rotation.prune_verified(source, export, invalid)
    assert source.sql == []


class Response:
    def __init__(self, body=b"", status=200):
        self.body, self.status_code = body, status
        self.headers = {"Content-Length": str(len(body))}
    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), chunk_size): yield self.body[i:i + chunk_size]
    def json(self): return json.loads(self.body)
    def close(self): pass


class PublicSession:
    def __init__(self): self.objects, self.calls = {}, []
    def get(self, url, **kwargs):
        assert "Authorization" not in kwargs.get("headers", {})
        assert kwargs.get("stream") is True and kwargs.get("allow_redirects") is False
        self.calls.append(url)
        return Response(self.objects[url])


class FakePublisher:
    def __init__(self, *, corrupt=False, stale=False):
        self.public = PublicSession()
        self.pointer, self.corrupt, self.stale = None, corrupt, stale
        self.uploads = []
    def request(self, method, path, *, payload):
        assert method == "POST" and path == "/releases"
        assert payload["tag_name"].startswith("archive-history-")
        return {"id": 12}
    def upload(self, release_id, name, path):
        body = Path(path).read_bytes()
        if self.corrupt and name == "history-04.tfarc": body = body[:-1] + bytes([body[-1] ^ 1])
        url = "https://github.com/S1rt3ge/tour-finder/releases/download/archive-history-generation-1/" + name
        self.public.objects[url] = body
        self.uploads.append(name)
        return url
    def publish_pointer(self, pointer):
        self.pointer = pointer
        self.public.objects[DEFAULT_MANIFEST_URL] = fmt.canonical_json({"old": True} if self.stale else pointer)
        return "c" * 40
    verify_public_pointer = rotation.Publisher.verify_public_pointer


def test_published_archive_is_downloaded_decrypted_and_verified_before_pointer(tmp_path):
    export = exported(tmp_path)
    publisher = FakePublisher()
    verified = rotation.publish_verified(export, publisher, key=KEY)
    assert verified.generation == export.generation
    assert len(publisher.uploads) == 66 and publisher.uploads[-1] == "manifest.json"
    assert len(publisher.public.calls) == 67  # 65 assets, manifest, pointer
    assert "deletion-plan.sqlite" not in publisher.uploads
    reader = ArchiveStore(manifest_url=DEFAULT_MANIFEST_URL, key=KEY, session=publisher.public,
                          cache_dir=tmp_path / "readback")
    assert reader.manifest()["generation"] == export.generation
    with reader.load_catalog() as conn:
        assert conn.execute("SELECT count(*) FROM offers").fetchone()[0] == 1


def test_corrupt_remote_ciphertext_prevents_manifest_or_pointer_publication(tmp_path):
    publisher = FakePublisher(corrupt=True)
    with pytest.raises(fmt.ArchiveError):
        rotation.publish_verified(exported(tmp_path), publisher, key=KEY)
    assert publisher.pointer is None and "manifest.json" not in publisher.uploads


def test_stale_public_pointer_never_produces_prune_receipt(tmp_path):
    with pytest.raises(fmt.ArchiveError, match="public_pointer_not_fresh"):
        rotation.publish_verified(exported(tmp_path), FakePublisher(stale=True), key=KEY)


def test_pointer_cas_rejects_race_before_writing_any_git_object():
    publisher = rotation.Publisher("fixture-unused")
    publisher.expected_head = "a" * 40
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path))
        return {"object": {"sha": "b" * 40}}
    publisher.request = request
    with pytest.raises(fmt.ArchiveError, match="concurrent_change"):
        publisher.publish_pointer({"generation": "later"})
    assert calls == [("GET", "/git/ref/heads/archive-index")]


def test_credentialed_requests_never_follow_redirect_to_another_host():
    class Session:
        def request(self, method, url, **kwargs):
            assert url.startswith(rotation.API + "/")
            assert kwargs["allow_redirects"] is False
            return Response(status=302)
    publisher = rotation.Publisher("fixture-unused", session=Session())
    with pytest.raises(fmt.ArchiveError, match="github_request_failed"):
        publisher.request("POST", "/releases", payload={})


def test_main_requires_explicit_prune_flag_before_any_network(monkeypatch, capsys):
    monkeypatch.delenv("ARCHIVE_PRUNE_ENABLED", raising=False)
    monkeypatch.setattr(rotation, "connect_source", lambda *_: pytest.fail("must not connect"))
    assert rotation.main(["--mode", "prune"]) == 1
    assert "archive_prune_not_enabled" in capsys.readouterr().out


def test_public_pointer_has_strict_stream_limit():
    publisher = rotation.Publisher("fixture-unused", public_session=PublicSession())
    publisher.public.objects[DEFAULT_MANIFEST_URL] = b" " * (rotation.MANIFEST_LIMIT + 1)
    with pytest.raises(fmt.ArchiveError, match="public_pointer_not_fresh"):
        publisher.verify_public_pointer({"generation": "fixture"})


def test_catalog_that_cannot_fit_reader_staging_is_never_published(tmp_path, monkeypatch):
    export = exported(tmp_path)
    publisher = FakePublisher()
    publisher.request = lambda *_args, **_kwargs: pytest.fail("must not create a release")
    monkeypatch.setattr(fmt, "encrypt_sqlite", lambda *_args, **_kwargs: {
        "kind": "catalog", "plaintext_bytes": 252 * fmt.MIB, "bytes": 115 * fmt.MIB})
    with pytest.raises(fmt.ArchiveError, match="archive_cache_capacity_exceeded"):
        rotation.publish_verified(export, publisher, key=KEY)
    assert publisher.uploads == [] and publisher.pointer is None


def test_every_asset_capacity_is_checked_before_any_publication(tmp_path, monkeypatch):
    export = exported(tmp_path)
    publisher = FakePublisher()
    publisher.request = lambda *_args, **_kwargs: pytest.fail("must not publish before the final shard passes")
    checked = []
    def seal(_path, _destination, **kwargs):
        return {"kind": kwargs["kind"], "shard": kwargs["shard"], "plaintext_bytes": 1, "bytes": 1}
    def capacity(descriptor):
        checked.append(descriptor["shard"])
        if descriptor["shard"] == 63:
            raise fmt.ArchiveError("archive_cache_capacity_exceeded")
        return 3
    monkeypatch.setattr(fmt, "encrypt_sqlite", seal)
    monkeypatch.setattr(rotation, "validate_asset_capacity", capacity)
    with pytest.raises(fmt.ArchiveError, match="archive_cache_capacity_exceeded"):
        rotation.publish_verified(export, publisher, key=KEY)
    assert checked == [None, *range(64)]
    assert publisher.uploads == [] and publisher.pointer is None


def test_export_progress_contains_only_phase_and_aggregate_counts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rotation, "PROGRESS_ROWS", 2)
    exported(tmp_path)
    progress = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    progress = [value for value in progress if value["stage"] == "export_progress"]
    assert [value["rows"] for value in progress if value["phase"] == "history"] == [0, 2, 4, 5]
    assert all(set(value) <= {"stage", "phase", "rows", "complete"} for value in progress)
