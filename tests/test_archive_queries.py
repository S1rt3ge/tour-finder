"""Unified reads use explicitly created temporary SQLite files only."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from tourfinder import archive_format as fmt
from tourfinder.archive_queries import ReadService, ReadUnavailable, parse_offer_id
from tourfinder.archive_store import ArchiveStore

NOW = datetime.now(timezone.utc)
STAMP = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
OLD = (NOW - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
DAY = (NOW + timedelta(days=10)).date().isoformat()
FILTERS = dict(date_from=DAY, date_till=DAY, adults=2, nights_min=5, nights_max=10)


def insert(conn, table, row):
    conn.execute(f"INSERT INTO {table}({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))


def offer(number=1, **changes):
    return dict(id=number, source="joinup", source_hotel_id=f"hotel-{number}", origin_id="RIX", origin_name="Riga",
                date_start=DAY, date_end=DAY, nights=7, board_code="AI", board_name="All inclusive",
                room_code="standard", room_name="Standard", room_placement="2AD", pax_adl=2,
                pax_chd=0, children_ages="", operator="joinup", link="https://example.test/offer",
                first_seen_at=OLD, last_seen_at=STAMP) | changes


def snapshot(number=1, offer_id=1, **changes):
    return dict(id=number, offer_id=offer_id, run_id=1, fetched_at=STAMP, price_cents=80000,
                currency="EUR", is_hot=0, availability=None, stop_sale=None,
                operator_avg_price_cents=None) | changes


def add(conn, row, price=80000, *, fetched_at=STAMP, review=True):
    conn.execute("INSERT OR IGNORE INTO hotels(source,source_hotel_id,name,category,country_id,country_name) VALUES (?,?,?,?,?,?)",
                 (row["source"], row["source_hotel_id"], "Hotel " + row["source_hotel_id"], "5", "TR", "Turkey"))
    insert(conn, "offers", row)
    insert(conn, "archive_offer_keys", dict(offer_id=row["id"], offer_key=fmt.offer_key(row)))
    insert(conn, "price_snapshots", snapshot(row["id"], row["id"], price_cents=price, fetched_at=fetched_at))
    if review:
        insert(conn, "hotel_reviews", dict(id=row["id"], source=row["source"], source_hotel_id=row["source_hotel_id"],
            platform="fixture", rating=4.5, rating_scale=5, reviews_count=100, match_status="ok", fetched_at=STAMP))
    conn.commit()


class LocalArchive(ArchiveStore):
    def __init__(self, path):
        self.path, self.calls, self.observations = path, [], {}
        self.broken = False

    def configured(self):
        return True

    def manifest(self):
        return dict(dataset_id="fixture", created_at=STAMP, catalog={"created_at": STAMP})

    @contextmanager
    def load_catalog(self):
        self.calls.append("catalog")
        if self.broken:
            raise fmt.ArchiveError("archive_ciphertext_mismatch")
        conn = fmt.open_readonly(self.path)

        class Catalog:
            manifest = self.manifest()
            execute = conn.execute

        try:
            yield Catalog()
        finally:
            conn.close()

    def history(self, key):
        self.calls.append("history")
        if self.broken:
            raise fmt.ArchiveError("archive_ciphertext_mismatch")
        return [dict(row, id=row["snapshot_id"], observation_key=fmt.observation_key(key, row), data_source="archive")
                for row in self.observations.get(key, [])]


@pytest.fixture
def stores(tmp_path):
    live_path, cold_path = tmp_path / "live.sqlite", tmp_path / "cold.sqlite"
    live = fmt.create_catalog(live_path, dataset_id="fixture", created_at=STAMP)
    # A live DB holds multiple observations; catalog projection holds one.
    live.execute("DROP TABLE price_snapshots")
    live.execute("""CREATE TABLE price_snapshots(id INTEGER PRIMARY KEY,offer_id INTEGER NOT NULL,run_id INTEGER,
        fetched_at TEXT NOT NULL,price_cents INTEGER NOT NULL,currency TEXT NOT NULL,is_hot INTEGER NOT NULL,
        availability TEXT,stop_sale TEXT,operator_avg_price_cents INTEGER)""")
    live.execute("CREATE INDEX idx_snapshots_latest ON price_snapshots(offer_id,fetched_at,id)")
    live.execute("CREATE UNIQUE INDEX uq_offer_identity ON offers(source,source_hotel_id,origin_id,date_start,nights,board_code,room_code,room_placement,pax_adl,pax_chd,children_ages)")
    live.commit()
    cold = fmt.create_catalog(cold_path, dataset_id="fixture", created_at=STAMP)
    traced = []

    def connect():
        conn = sqlite3.connect(live_path)
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(traced.append)
        return conn

    archive = LocalArchive(cold_path)
    yield live, cold, archive, ReadService(connect=connect, archive_store=archive), traced
    live.close()
    cold.close()


def test_empty_live_database_search_uses_explicit_stale_archive(stores):
    _live, cold, _archive, service, _trace = stores
    add(cold, offer(last_seen_at=OLD), fetched_at=OLD)
    result = service.search(group=False, **FILTERS)
    row = result["results"][0]
    assert row["offer_id"].startswith("a_") and row["archived"] and row["stale"]
    assert row["archive_as_of"] == STAMP and row["snapshots_count"] is None
    assert row["board_label"] == "Всё включено" and row["review_rating"] == 4.5
    assert result["storage"]["mode"] == "archive" and result["storage"]["live"] == "empty"


def test_full_live_page_avoids_cold_network_and_scan(stores):
    live, cold, archive, service, _trace = stores
    add(live, offer())
    add(cold, offer(2), price=100)
    result = service.search(group=False, limit=1, **FILTERS)
    assert result["results"][0]["offer_id"] == 1
    assert result["storage"]["archive"] == "not_needed" and not archive.calls


def test_search_includes_coverage_for_exact_filters_without_scheduling(stores, monkeypatch):
    import tourfinder.archive_queries as module
    live, _cold, archive, service, trace = stores
    add(live, offer())
    received = []
    evidence = {"state": "partial", "complete": False, "reasons": ["stays_not_recorded"]}

    def coverage(conn, filters):
        received.append(dict(filters))
        return evidence

    monkeypatch.setattr(module, "get_search_coverage", coverage)
    result = service.search(**FILTERS, boards="AI", stars_min=4, limit=1)
    assert result["coverage"] == evidence and result["count"] == 1
    assert received[0]["adults"] == 2 and received[0]["boards"] == "AI"
    assert received[0]["stars_min"] == 4 and received[0]["date_from"] == DAY
    assert not archive.calls
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in trace)


def test_coverage_failure_is_unknown_without_hiding_readable_offers(stores, monkeypatch):
    import tourfinder.archive_queries as module
    live, _cold, _archive, service, _trace = stores
    add(live, offer())

    def unavailable(conn, filters):
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(module, "get_search_coverage", unavailable)
    result = service.search(**FILTERS, limit=1)
    assert result["count"] == 1 and result["coverage"]["state"] == "unavailable"
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["reasons"] == ["coverage_read_failed"]
    assert result["storage"]["partial"] is False


def test_empty_budget_search_keeps_known_cold_party_without_second_catalog_load(stores):
    _live, cold, archive, service, _trace = stores
    add(cold, offer(), price=150000)
    result = service.search(group=False, budget_max=1000, **FILTERS)
    assert result["results"] == []
    assert result["available_compositions"] == [dict(
        pax_adl=2, pax_chd=0, children_ages="", offers=1,
        live_offers=0, archived_offers=1)]
    assert archive.calls == ["catalog"]
    assert result["storage"]["live"] == "empty" and result["storage"]["archive"] == "ok"
    assert result["storage"]["partial"] is False
    assert result["storage"]["partial_reasons"] == []


def test_empty_search_known_party_counts_do_not_double_count_live_and_archive(stores):
    live, cold, archive, service, _trace = stores
    add(live, offer(), price=150000)
    add(cold, offer(), price=150000)
    result = service.search(group=False, budget_max=1000, **FILTERS)
    composition = result["available_compositions"][0]
    assert composition["offers"] is None
    assert composition["live_offers"] == composition["archived_offers"] == 1
    assert archive.calls == ["catalog"]
    assert result["storage"]["partial"] is False
    assert result["storage"]["partial_reasons"] == []


@pytest.mark.parametrize("candidate_limit,partial", [(2, True), (3, False)])
def test_empty_search_after_stale_live_identity_suppression_reports_only_real_truncation(stores, monkeypatch, candidate_limit, partial):
    import tourfinder.archive_queries as module
    live, cold, _archive, service, _trace = stores
    for i in range(1, 4):
        row = offer(i, pax_adl=1, room_placement="1AD", last_seen_at=OLD)
        add(live, row, fetched_at=OLD)
        add(cold, row, fetched_at=OLD)
    monkeypatch.setattr(module, "CANDIDATE_LIMIT", candidate_limit)
    result = service.search(group=False, **(FILTERS | {"adults": 1}))
    assert result["count"] == 0 and result["results"] == []
    assert result["storage"]["live"] == "empty" and result["storage"]["archive"] == "ok"
    assert result["storage"]["partial"] is partial
    assert result["storage"]["partial_reasons"] == (["archive_candidate_limit"] if partial else [])
    assert result["available_compositions"][0]["pax_adl"] == 1


@pytest.mark.parametrize("failing_source", ["live", "archive", "both"])
def test_empty_search_composition_failure_has_safe_deduplicated_reason(stores, monkeypatch, failing_source):
    import tourfinder.archive_queries as module
    _live, _cold, _archive, service, _trace = stores
    original = module.queries.available_compositions

    def compositions(conn):
        source = "live" if isinstance(conn, sqlite3.Connection) else "archive"
        if failing_source in {source, "both"}:
            raise RuntimeError("private connection details must never enter metadata")
        return original(conn)

    monkeypatch.setattr(module.queries, "available_compositions", compositions)
    result = service.search(group=False, **FILTERS)
    assert result["count"] == 0 and result["results"] == []
    assert result["storage"]["live"] == "empty" and result["storage"]["archive"] == "ok"
    assert result["storage"]["partial"] is True
    assert result["storage"]["partial_reasons"] == ["composition_lookup_failed"]
    assert "private connection" not in repr(result)


def test_empty_search_without_configured_archive_is_not_partial(stores, monkeypatch):
    _live, _cold, archive, service, _trace = stores
    monkeypatch.setattr(archive, "configured", lambda: False)
    result = service.search(group=False, **FILTERS)
    assert result["count"] == 0 and result["results"] == []
    assert result["storage"]["live"] == "empty" and result["storage"]["archive"] == "not_configured"
    assert result["storage"]["partial"] is False
    assert result["storage"]["partial_reasons"] == []


def test_live_expensive_price_blocks_old_cheap_price_before_budget_filter(stores):
    live, cold, _archive, service, trace = stores
    same = offer()
    add(live, same, price=150000)
    add(cold, same, price=80000, fetched_at=OLD)
    add(cold, offer(2), price=90000)
    result = service.search(group=False, budget_max=1000, **FILTERS)
    assert [row["source_hotel_id"] for row in result["results"]] == ["hotel-2"]
    probes = [sql for sql in trace if sql.startswith("SELECT * FROM offers WHERE (")]
    assert probes and all("price_cents" not in sql and "last_seen_at" not in sql for sql in probes)


def test_stale_live_identity_also_blocks_cold_and_different_room_survives(stores):
    live, cold, _archive, service, _trace = stores
    add(live, offer(last_seen_at=OLD), price=150000, fetched_at=OLD)
    add(cold, offer(), price=80000)
    add(cold, offer(2, source_hotel_id="hotel-1", room_code="suite"), price=85000, review=False)
    result = service.search(group=False, budget_max=1000, **FILTERS)
    assert len(result["results"]) == 1 and result["results"][0]["room_code"] == "suite"


def test_reused_numeric_id_does_not_replace_archived_detail_or_history(stores):
    live, cold, archive, service, _trace = stores
    old_offer = offer()
    add(cold, old_offer)
    add(live, offer(source_hotel_id="different-live-hotel"), price=199900)
    key = fmt.offer_key(old_offer)
    archive.observations[key] = [dict(snapshot_id=1, **{k: v for k, v in snapshot().items() if k not in {"id", "offer_id"}})]
    detail = service.detail("a_" + key)["offer"]
    assert detail["source_hotel_id"] == "hotel-1" and detail["price_cents"] == 80000 and detail["archived"]
    history = service.history("a_" + key)
    assert [row["price_cents"] for row in history["history"]] == [80000]
    assert service.detail(1)["offer"]["source_hotel_id"] == "different-live-hotel"


def test_archive_identifier_resolves_new_live_id_for_same_natural_offer(stores):
    live, cold, _archive, service, _trace = stores
    old_offer = offer()
    add(cold, old_offer)
    add(live, old_offer | {"id": 99}, price=95000)
    row = service.detail("a_" + fmt.offer_key(old_offer))["offer"]
    assert row["offer_id"] == 99 and row["price_cents"] == 95000 and not row["archived"]


def test_history_merges_fingerprints_prefers_live_and_keeps_same_time_changes(stores):
    live, cold, archive, service, _trace = stores
    value = offer()
    add(live, value)
    add(cold, value)
    key = fmt.offer_key(value)
    base = {k: v for k, v in snapshot().items() if k not in {"id", "offer_id"}}
    archive.observations[key] = [dict(base, snapshot_id=500, run_id=99),
                                 dict(base, snapshot_id=501, price_cents=75000),
                                 dict(base, snapshot_id=502, currency="USD", price_cents=999999)]
    result = service.history(1)
    assert result["history_total"] == 3
    assert next(row for row in result["history"] if row["price_cents"] == 80000)["data_source"] == "live"
    assert result["statistics"]["avg_seen_cents"] == 77500  # EUR only
    assert result["statistics"]["complete"]


def test_same_time_recycled_snapshot_id_keeps_current_live_price_last(stores):
    live, cold, archive, service, _trace = stores
    value = offer()
    add(live, value, price=95000)
    add(cold, value, price=150000)
    # Two live updates at one timestamp retain their deterministic ID order.
    insert(live, "price_snapshots", snapshot(2, price_cents=90000))
    live.commit()
    key = fmt.offer_key(value)
    archived = {k: v for k, v in snapshot().items() if k not in {"id", "offer_id"}}
    archive.observations[key] = [dict(archived, snapshot_id=9999, price_cents=150000)]
    result = service.history("a_" + key)
    assert [row["price_cents"] for row in result["history"]] == [150000, 95000, 90000]
    assert result["history"][-1]["data_source"] == "live"
    assert result["history"][-1]["price_cents"] == service.detail("a_" + key)["offer"]["price_cents"]


def test_grouped_search_combines_only_missing_live_variants(stores):
    live, cold, _archive, service, _trace = stores
    add(live, offer(), price=90000)
    add(cold, offer(), price=50000)
    add(cold, offer(2, source_hotel_id="hotel-1", room_code="suite"), price=95000, review=False)
    result = service.search(group=True, **FILTERS)
    assert len(result["results"]) == 1
    row = result["results"][0]
    assert row["price_cents"] == 90000 and row["variants"] == 2
    assert row["variants_min_cents"] == 90000 and row["variants_max_cents"] == 95000
    assert row["archived_variants"] == 1


def test_down_database_falls_back_but_down_and_corrupt_is_explicit_failure(stores):
    _live, cold, archive, _service, _trace = stores
    add(cold, offer())

    def broken():
        raise RuntimeError("postgresql://private-user:secret-token@host")

    service = ReadService(connect=broken, archive_store=archive)
    result = service.search(group=False, **FILTERS)
    assert result["storage"]["live"] == "unavailable" and result["storage"]["partial"]
    assert result["storage"]["partial_reasons"] == []
    assert result["results"][0]["stale"]
    archive.broken = True
    with pytest.raises(ReadUnavailable) as error:
        service.search(**FILTERS)
    assert str(error.value) == "read_sources_unavailable"


def test_corrupt_archive_does_not_hide_successful_live_results(stores):
    live, _cold, archive, service, _trace = stores
    add(live, offer())
    archive.broken = True
    result = service.search(group=False, **FILTERS)
    assert result["count"] == 1 and result["storage"]["archive"] == "unavailable"
    assert result["storage"]["partial"]
    assert result["storage"]["archive_error"] == "archive_read_failed"
    assert result["storage"]["partial_reasons"] == []


def test_missing_tables_are_unavailable_instead_of_fake_empty(tmp_path, stores):
    _live, cold, archive, _service, _trace = stores
    add(cold, offer())

    def empty_schema():
        return sqlite3.connect(tmp_path / "no-tables.sqlite")

    service = ReadService(connect=empty_schema, archive_store=archive)
    assert service.search(**FILTERS)["storage"]["live"] == "unavailable"


def test_cold_candidate_limit_is_explicit_and_compositions_do_not_double_count(stores, monkeypatch):
    import tourfinder.archive_queries as module
    live, cold, _archive, service, _trace = stores
    add(live, offer(20))
    for i in range(1, 4):
        add(cold, offer(i), price=80000 + i)
    monkeypatch.setattr(module, "CANDIDATE_LIMIT", 2)
    result = service.search(group=False, **FILTERS)
    assert result["storage"]["partial"] and result["count"] == 3
    assert result["storage"]["partial_reasons"] == ["archive_candidate_limit"]
    composition = service.compositions()["compositions"][0]
    assert composition["offers"] is None
    assert composition["live_offers"] == 1 and composition["archived_offers"] == 3


def test_options_reads_all_configured_stores_even_when_live_is_nonempty(stores):
    live, cold, archive, service, _trace = stores
    add(cold, offer())
    result = service.options()
    assert result["countries"] == [{"country_id": "TR", "country_name": "Turkey"}]
    assert result["boards"] == [{"board_code": "AI", "board_name": "All inclusive"}]
    assert result["storage"]["mode"] == "archive"
    archive.calls.clear()
    add(live, offer())
    assert service.options()["storage"]["archive"] == "ok"
    assert archive.calls == ["catalog"]


def test_options_exposes_archive_only_filters_and_live_labels_win(stores):
    live, cold, _archive, service, _trace = stores
    add(live, offer())
    add(cold, offer(2, board_code="BB", board_name="Breakfast"))
    cold.execute("UPDATE hotels SET country_id='EG',country_name='Egypt'")
    cold.commit()
    result = service.options()
    assert {r["country_id"] for r in result["countries"]} == {"TR", "EG"}
    assert {r["board_code"] for r in result["boards"]} == {"AI", "BB"}
    assert service.search(**FILTERS, countries="EG", boards="BB")["count"] == 1


def test_broken_archive_dictionary_keeps_live_choices_with_partial_status(stores):
    live, _cold, archive, service, _trace = stores
    add(live, offer())
    archive.broken = True
    result = service.options()
    assert result["countries"] == [{"country_id": "TR", "country_name": "Turkey"}]
    assert result["boards"][0]["board_code"] == "AI"
    assert result["storage"]["archive"] == "unavailable"
    assert result["storage"]["partial"]


def test_archive_search_preserves_euro_contract_and_live_currency_precedence(stores):
    live, cold, _archive, service, _trace = stores
    add(live, offer(), price=100)
    live.execute("UPDATE price_snapshots SET currency='USD'")
    live.commit()
    add(cold, offer(), price=100)
    add(cold, offer(2), price=200)
    cold.execute("UPDATE price_snapshots SET currency='USD' WHERE offer_id=2")
    add(cold, offer(3), price=90000)
    result = service.search(group=False, **FILTERS, budget_max=1000)
    assert len(result["results"]) == 1
    assert result["results"][0]["source_hotel_id"] == "hotel-3"
    assert result["results"][0]["currency"] == "EUR"


def test_search_releases_live_connection_before_archive_io(stores, monkeypatch):
    _live, cold, archive, service, _trace = stores
    add(cold, offer())
    original_connect, original_catalog = service._connect, archive.load_catalog
    active = []

    class TrackedConnection:
        def __init__(self):
            self.conn = original_connect()
            active.append(self)

        def execute(self, *args):
            return self.conn.execute(*args)

        def close(self):
            self.conn.close()
            active.remove(self)

    @contextmanager
    def catalog_without_live_connection():
        assert not active, "Archive I/O must not retain a live DB connection"
        with original_catalog() as catalog:
            yield catalog
        assert not active

    monkeypatch.setattr(service, "_connect", TrackedConnection)
    monkeypatch.setattr(archive, "load_catalog", catalog_without_live_connection)
    result = service.search(**FILTERS)
    assert result["count"] == 1 and not active


def test_failed_identity_reconnect_cannot_resurrect_old_archive_price(stores, monkeypatch):
    live, cold, _archive, service, _trace = stores
    add(live, offer(last_seen_at=OLD), price=150000)
    add(cold, offer(last_seen_at=OLD), price=100)
    original_connect = service._connect
    calls = 0

    def reconnect_fails():
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_connect()
        raise RuntimeError("fixture secret must not appear in API")

    monkeypatch.setattr(service, "_connect", reconnect_fails)
    result = service.search(**FILTERS, budget_max=1000)
    assert result["count"] == 0 and calls == 2
    assert result["storage"]["partial"]
    assert result["storage"]["archive_error"] == "live_identity_check_failed"


def test_options_live_labels_take_priority_when_cold_fills_missing_dictionary(stores):
    live, cold, _archive, service, _trace = stores
    live.execute("INSERT INTO hotels(source,source_hotel_id,name,country_id,country_name) VALUES ('joinup','live','Hotel','TR','Live Turkey')")
    live.commit()
    add(cold, offer())
    result = service.options()
    assert result["countries"][0]["country_name"] == "Live Turkey"
    assert result["boards"][0]["board_code"] == "AI"
    assert result["storage"]["mode"] == "mixed"


def test_history_statistics_include_all_observations_before_display_truncation(stores):
    live, cold, archive, service, _trace = stores
    value = offer()
    add(live, value)
    add(cold, value)
    key = fmt.offer_key(value)
    base = {k: v for k, v in snapshot().items() if k not in {"id", "offer_id"}}
    archive.observations[key] = [dict(base, snapshot_id=100 + i, price_cents=100 + i,
        fetched_at=(NOW - timedelta(seconds=4000 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")) for i in range(2500)]
    result = service.history(1)
    assert result["history_total"] == 2501 and len(result["history"]) == 2000
    assert result["history_truncated"] and result["statistics"]["min_seen_cents"] == 100


def test_down_database_and_unconfigured_archive_is_never_empty_success(stores, monkeypatch):
    monkeypatch.delenv("APP_ARCHIVE_KEY", raising=False)
    monkeypatch.delenv("TOUR_ARCHIVE_MANIFEST_URL", raising=False)

    def broken():
        raise RuntimeError("do not print a connection string")

    service = ReadService(connect=broken)
    for action in (lambda: service.search(**FILTERS), service.options, service.compositions):
        with pytest.raises(ReadUnavailable, match="read_sources_unavailable"):
            action()


@pytest.mark.parametrize("value", [0, -1, True, "", "a_bad", "1 OR 1=1", "../1", "9223372036854775808", "١"])
def test_invalid_offer_ids_never_reach_sql(value):
    with pytest.raises((ValueError, fmt.ArchiveError)):
        parse_offer_id(value)
