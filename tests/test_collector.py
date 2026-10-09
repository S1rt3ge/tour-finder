"""Collector regression tests: explicit temporary SQLite and fake sources only."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event

from tourfinder import cli, db, fetcher, subscriptions


NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    # Never call db.connect/get_engine: DATABASE_URL and data/tourfinder.db are
    # deliberately irrelevant, even when a developer has production env set.
    engine = create_engine(f"sqlite:///{(tmp_path / 'collector.sqlite').as_posix()}")
    db.metadata.create_all(engine)
    connection = db.DB(engine)
    yield connection
    connection.close()
    engine.dispose()


def add_run(conn, source="joinup", tier="near", pax="2", origin="RIX", *, started=None,
            finished=True, errors=None, owner=None, extra_params=None):
    started = started or NOW - timedelta(minutes=10)
    params = {"source": source, ("origin" if source == "joinup" else "departureAirport"): origin, **(extra_params or {})}
    if owner is not None:
        params["collector_owner"] = owner
    run_id = conn.execute(
        "INSERT INTO fetch_runs(started_at,finished_at,tier,pax_spec,params,errors) "
        "VALUES (:s,:f,:t,:p,:params,:errors) RETURNING id",
        {"s": started.isoformat(), "f": started.isoformat() if finished else None,
         "t": tier, "p": pax, "params": json.dumps(params),
         "errors": json.dumps(errors) if errors is not None else None}).fetchone()["id"]
    conn.commit()
    return run_id


def raw_waavo(number, price=100, *, operator="teztour", link=None, reviews=False):
    hotel = {"id": number, "name": f"Fixture hotel {number}", "starsCount": 4}
    if reviews:
        hotel["tripadvisor"] = {"rating": 4.2, "ratingsCount": 37}
    return {"hotel": hotel, "operator": {"code": operator},
            "room": {"name": "Standard", "meal": {"group": {"code": "AI"}}},
            "date": "2026-10-20", "duration": 7,
            "pricing": {"price": price, "currency": "EUR"},
            "hotelUrl": link or f"https://example.invalid/hotel/{number}"}


class WaavoFixture:
    def __init__(self, rows):
        self.rows = rows
        self.requests_made = 0

    def search_pages(self, *args, **kwargs):
        self.requests_made += 1
        yield from self.rows


def test_waavo_run_records_exact_request_bounds_for_coverage(conn):
    client = WaavoFixture([])
    client.search_pages = Mock(return_value=iter([]))
    result = fetcher.run_waavo_fetch(conn, client, adults=1, children_ages=[7],
                                    days_from=8, days_till=14, tier="mid", pax_spec="1+1:7")
    row = conn.execute("SELECT params FROM fetch_runs WHERE id=:id",
                       {"id": result["run_id"]}).fetchone()
    params = json.loads(row["params"])
    args, kwargs = client.search_pages.call_args
    assert params["departureAirport"] == "RIX"
    assert (params["dateFrom"], params["dateTo"], params["adults"]) == args
    assert params["children_ages"] == kwargs["children_ages"] == [7]
    assert params["durationFrom"] == kwargs["duration_from"] == 2
    assert params["durationTo"] == kwargs["duration_till"] == 21
    assert params["max_pages"] == kwargs["max_pages"] is None


@pytest.mark.parametrize("origin", ["VNO", "TLL"])
def test_waavo_airport_propagates_to_request_metadata_and_stored_identity(conn, origin):
    client = WaavoFixture([raw_waavo(1)])  # Missing echo must use requested origin.
    original = client.search_pages
    client.search_pages = Mock(side_effect=original)
    result = fetcher.run_waavo_fetch(conn, client, origin=origin, tier="near", pax_spec="2")
    assert result["completed"] and client.search_pages.call_args.kwargs["origin"] == origin
    assert conn.execute("SELECT origin_id FROM offers").scalar() == origin
    params = json.loads(conn.execute("SELECT params FROM fetch_runs").scalar())
    assert params["departureAirport"] == origin
    assert ("waavo", "near", "2", origin) in cli._run_history(conn)


def test_same_offer_terms_from_different_airports_never_collapse(conn):
    for origin in ("RIX", "VNO", "TLL"):
        result = fetcher.run_waavo_fetch(conn, WaavoFixture([raw_waavo(1)]), origin=origin)
        assert result["completed"]
    assert conn.execute("SELECT count(*) FROM offers").scalar() == 3
    assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == 3


@pytest.mark.parametrize("origin,identifier", [("VNO", "2151"), ("TLL", "2552")])
def test_joinup_fetch_maps_requests_echoes_and_deeplink_to_same_airport(conn, origin, identifier):
    class DirectFixture:
        lang = "lv"
        requests_made = 0

        def destinations(self, requested):
            assert requested == identifier
            return [{"id": "c_9"}]

        def stays(self, requested, destination, dates):
            assert requested == identifier
            return [7]

        def search_pages(self, requested, *args, **kwargs):
            assert requested == identifier
            if kwargs.get("tour_types"):
                return
            yield {"hotel": {"id": "fixture", "name": "Fixture"}, "offers": [{
                "from": {}, "date_start": "2026-10-20", "stay": {"stay": 7},
                "board": {"board_type": "AI"}, "price": {"total_price": {"price": 100}}}]}
    result = fetcher.run_fetch(conn, DirectFixture(), origin=origin, tier="near", pax_spec="2")
    assert result["completed"]
    row = conn.execute("SELECT origin_id,link FROM offers").fetchone()
    assert row["origin_id"] == identifier and f"origin={identifier}" in row["link"]
    assert ("joinup", "near", "2", origin) in cli._run_history(conn)


def test_mismatching_airport_never_writes_the_mislabelled_offer(conn):
    raw = raw_waavo(1)
    raw["departureAirport"] = {"code": "RIX"}
    result = fetcher.run_waavo_fetch(conn, WaavoFixture([raw]), origin="VNO")
    assert not result["completed"]
    assert conn.execute("SELECT count(*) FROM offers").scalar() == 0


def test_legacy_riga_success_cannot_refresh_vilnius_or_tallinn(conn):
    run = add_run(conn, "waavo", "near", "2")
    conn.execute("UPDATE fetch_runs SET params=:params WHERE id=:id",
                 {"params": json.dumps({"source": "waavo"}), "id": run})
    conn.commit()
    scopes = [(origin, "2") for origin in ("RIX", "VNO", "TLL")]
    due = {task.key for task in cli.plan_collection(conn, scopes, NOW)}
    assert ("waavo", "near", "2", "RIX") not in due
    assert ("waavo", "near", "2", "VNO") in due and ("waavo", "near", "2", "TLL") in due


def test_duration_estimate_does_not_borrow_another_airports_timing(conn):
    timed_run(conn, collect_task(origin="RIX"), 200)
    assert cli._duration_estimate(collect_task(origin="VNO"), cli._run_history(conn), NOW) is None


def test_active_airport_party_scopes_preserve_each_users_request_and_subscription(conn, monkeypatch):
    from tourfinder import collection_requests
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "100")
    monkeypatch.setattr(collection_requests, "active_scopes", lambda conn, now=None: [
        {"origins": "VNO", "adults": 1, "date_from": "2026-10-09", "date_till": "2026-10-29"},
        {"origins": "TLL", "adults": 4, "children_ages": "8,6", "date_from": "2026-10-09", "date_till": "2026-10-29"}])
    saved_party(conn, "100", 5, changes={"origins": "VNO,TLL"})
    saved_party(conn, "denied", 6, changes={"origins": "TLL"})
    request_party(conn, "6")  # Legacy global requests retain Riga semantics.
    active = set(cli.active_collection_scopes(conn, NOW))
    assert {("VNO", "1"), ("TLL", "4+2:6,8"), ("VNO", "5"), ("TLL", "5"), ("RIX", "6")} <= active
    assert not {("TLL", "1"), ("VNO", "4+2:6,8"), ("RIX", "1"), ("RIX", "5"), ("TLL", "6")} & active
    assert {("RIX", spec) for spec in cli.DEFAULT_PAX} <= active


def test_entirely_outside_horizon_subscriptions_do_not_activate_airports(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "100")
    for origin in ("RIX", "VNO", "TLL"):
        saved_party(conn, "100", 5, changes={"origins": origin, "date_from": "2026-12-01", "date_till": "2026-12-20"})
    assert cli.active_collection_scopes(conn, NOW) == [("RIX", spec) for spec in cli.DEFAULT_PAX]


def test_real_durable_request_reader_only_activates_approved_nonexpired_exact_pairs(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "100")
    for owner, origin, adult, expiry in [("100", "VNO", 1, NOW+timedelta(days=1)),
        ("100", "TLL", 4, NOW+timedelta(days=1)), ("denied", "TLL", 6, NOW+timedelta(days=1)),
        ("100", "VNO", 5, NOW-timedelta(seconds=1))]:
        scope = {"origins": origin, "adults": adult, "children_ages": "", "date_from": "2026-10-09", "date_till": "2026-10-20"}
        conn.execute("""INSERT INTO collection_requests(owner_id,request_key,filters,created_at,updated_at,expires_at)
            VALUES (:owner,:key,:filters,:now,:now,:expiry)""", {"owner": owner, "key": f"{owner}-{origin}-{adult}",
            "filters": json.dumps(scope), "now": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")})
    conn.commit()
    scopes = set(cli.active_collection_scopes(conn, NOW))
    assert scopes == {("RIX", spec) for spec in cli.DEFAULT_PAX} | {("VNO", "1"), ("TLL", "4")}
    keys = {task.key for task in cli._collect_tasks(scopes)}
    assert ("waavo", "near", "1", "VNO") in keys and ("joinup", "far", "4", "TLL") in keys
    assert ("waavo", "near", "1", "TLL") not in keys and ("joinup", "far", "4", "VNO") not in keys


class CollectorDialectProxy:
    """Exercise PG batch selection using the same portable SQL on local SQLite.

    This verifies contents/transactions/command counts, not PostgreSQL latency.
    """
    def __init__(self, connection, dialect="postgresql", *, fail_snapshot_batch=None):
        self.connection, self.dialect = connection, dialect
        self.commands = []
        self.commits = self.rollbacks = self.snapshot_batches = 0
        self.fail_snapshot_batch = fail_snapshot_batch

    def execute(self, statement, params=None):
        self.commands.append((statement, len(params or {})))
        if statement.startswith("INSERT INTO price_snapshots"):
            self.snapshot_batches += 1
            if self.snapshot_batches == self.fail_snapshot_batch:
                raise RuntimeError("fixture private diagnostic must not be logged")
        return self.connection.execute(statement, params)

    def commit(self):
        self.connection.commit()
        self.commits += 1

    def rollback(self):
        self.connection.rollback()
        self.rollbacks += 1


@pytest.mark.parametrize("dialect,size,batches", [("sqlite", 40, 25), ("postgresql", 200, 5)])
def test_dialect_batch_size_reduces_commands_with_identical_complete_observations(conn, caplog, dialect, size, batches):
    proxy = CollectorDialectProxy(conn, dialect)
    with caplog.at_level("INFO", logger="tourfinder.fetcher"):
        result = fetcher.run_waavo_fetch(proxy, WaavoFixture(
            [raw_waavo(index, reviews=True) for index in range(1000)]), tier="near", pax_spec="2")
    assert result["completed"] and result["offers_seen"] == 1000
    assert len(proxy.commands) == 2 + batches * 5  # start/end + five SQL statements per full Waavo batch
    assert proxy.commits == batches + 2 and proxy.rollbacks == 0
    assert max(count for _, count in proxy.commands) == size * 19
    assert conn.execute("SELECT count(*) FROM hotels").scalar() == 1000
    assert conn.execute("SELECT count(*) FROM offers").scalar() == 1000
    assert conn.execute("SELECT count(*) FROM hotel_reviews").scalar() == 1000
    assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == 1000
    assert conn.execute("SELECT count(DISTINCT offer_id) FROM price_snapshots").scalar() == 1000
    assert conn.execute("SELECT count(*) FROM price_snapshots WHERE price_cents<>10000").scalar() == 0
    metrics = [record for record in caplog.records if "write batches:" in record.msg]
    assert len(metrics) == 1
    assert metrics[0].args[:4] == (result["run_id"], size, batches, batches)
    assert metrics[0].args[4] >= 0
    assert "Fixture hotel" not in metrics[0].getMessage() and "example.invalid" not in metrics[0].getMessage()


def test_pg_batch_boundary_preserves_first_price_last_link_and_repeated_observations(conn):
    proxy = CollectorDialectProxy(conn)
    rows = [raw_waavo(index, 100) for index in range(201)] + [
        raw_waavo(0, 80, link="https://example.invalid/updated")]
    first = fetcher.run_waavo_fetch(proxy, WaavoFixture(rows))
    assert first["completed"] and first["offers_seen"] == 201
    matching = conn.execute("""SELECT o.id,o.link,p.price_cents FROM offers o
        JOIN price_snapshots p ON p.offer_id=o.id WHERE source_hotel_id='teztour:0'""").fetchone()
    assert matching["price_cents"] == 10000 and matching["link"] == "https://example.invalid/updated"
    second = fetcher.run_waavo_fetch(proxy, WaavoFixture([raw_waavo(index, 100) for index in range(201)]))
    assert second["completed"] and second["offers_seen"] == 201
    assert conn.execute("SELECT count(*) FROM offers").scalar() == 201
    assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == 402
    assert [row["n"] for row in conn.execute(
        "SELECT count(*) AS n FROM price_snapshots GROUP BY run_id ORDER BY run_id")] == [201, 201]
    assert conn.execute("""SELECT count(*) FROM
        (SELECT run_id,offer_id FROM price_snapshots GROUP BY run_id,offer_id HAVING count(*)<>1) duplicates""").scalar() == 0


def test_pg_hot_pass_crosses_batch_boundary_without_replacing_price(conn):
    class JoinUpFixture:
        lang = "lv"
        requests_made = 0

        def destinations(self, origin):
            return [{"id": "country"}]

        def stays(self, *args):
            return [7]

        def search_pages(self, *args, **kwargs):
            self.requests_made += 1
            price = 80 if kwargs.get("tour_types") else 100
            for index in range(205):
                yield {"hotel": {"id": str(index), "name": f"Fixture {index}"}, "offers": [{
                    "from": {"id": "3164"}, "date_start": "2026-10-20",
                    "stay": {"stay": 7}, "board": {"board_type": "AI"},
                    "price": {"total_price": {"price": price}}}]}

    result = fetcher.run_fetch(CollectorDialectProxy(conn), JoinUpFixture())
    assert result["completed"] and result["offers_seen"] == 205
    assert conn.execute("SELECT count(*) FROM offers").scalar() == 205
    assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == 205
    assert conn.execute("SELECT count(*) FROM price_snapshots WHERE price_cents=10000 AND is_hot=1").scalar() == 205


def test_pg_second_batch_failure_preserves_first_and_rolls_back_every_current_table(conn, caplog):
    proxy = CollectorDialectProxy(conn, fail_snapshot_batch=2)
    with caplog.at_level("INFO", logger="tourfinder.fetcher"):
        result = fetcher.run_waavo_fetch(proxy, WaavoFixture(
            [raw_waavo(index, reviews=True) for index in range(450)]), tier="near", pax_spec="2")
    assert not result["completed"] and result["offers_seen"] == 200
    for table in ("hotels", "offers", "hotel_reviews", "price_snapshots"):
        assert conn.execute(f"SELECT count(*) FROM {table}").scalar() == 200
    assert proxy.rollbacks >= 1 and proxy.commits == 3  # start, first batch, incomplete end
    persisted = conn.execute("SELECT finished_at,errors FROM fetch_runs").fetchone()
    assert persisted["finished_at"] and json.loads(persisted["errors"]) == ["RuntimeError"]
    assert cli._run_history(conn)[("waavo", "near", "2", "RIX")]["succeeded"] is None
    metric = next(record for record in caplog.records if "write batches:" in record.msg)
    assert metric.args[:4] == (result["run_id"], 200, 2, 1)
    assert "private diagnostic" not in caplog.text


def test_flush_metrics_include_commit_and_rollback_time_only(conn, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: clock[0])
    writer = fetcher._BatchWriter(CollectorDialectProxy(conn), 123)
    store = Mock(side_effect=[7, RuntimeError("fixture")])
    def timed_store(*args):
        clock[0] += 2
        return store(*args)
    def commit():
        clock[0] += 0.5
    def rollback():
        clock[0] += 0.25
    monkeypatch.setattr(fetcher, "_store_batch", timed_store)
    monkeypatch.setattr(writer.conn, "commit", commit)
    monkeypatch.setattr(writer.conn, "rollback", rollback)
    writer.entries = ["local fixture"]
    clock[0] = 100  # Time outside a flush must not enter write timing.
    writer.flush()
    clock[0] += 100
    writer.entries = ["local fixture"]
    with pytest.raises(RuntimeError):
        writer.flush()
    writer.flush()  # Empty flush adds neither time nor a commit.
    assert writer.flush_count == 2 and writer.flush_committed == 1
    assert writer.flush_seconds == 4.75 and writer.offers_seen == 7


@pytest.mark.parametrize("source", ["joinup", "waavo"])
def test_source_deadline_aborts_task_and_flushes_acquired_rows(conn, monkeypatch, source):
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: 0)
    called = []
    class DeadlineFixture:
        lang = "lv"
        requests_made = 0

        def destinations(self, origin):
            assert self.deadline == 123
            return [{"id": "first"}, {"id": "must-not-be-requested"}]

        def stays(self, origin, destination, dates):
            called.append(destination)
            return [7]

        def search_pages(self, *args, **kwargs):
            assert self.deadline == 123
            self.requests_made += 1
            if source == "waavo":
                yield raw_waavo(1)
                raise fetcher.waavo.WaavoDeadlineError("partial: collection time budget exhausted")
            yield {"hotel": {"id": "1", "name": "Fixture"}, "offers": [{
                "from": {"id": "3164"}, "date_start": "2026-10-20", "stay": {"stay": 7},
                "board": {"board_type": "AI"}, "price": {"total_price": {"price": 100}}}]}
            raise fetcher.joinup.JoinUpDeadlineError("partial: collection time budget exhausted")

    fetch = fetcher.run_fetch if source == "joinup" else fetcher.run_waavo_fetch
    result = fetch(CollectorDialectProxy(conn), DeadlineFixture(), deadline=123, tier="near", pax_spec="2")
    assert not result["completed"] and result["offers_seen"] == 1
    assert result["errors"] == ["partial: collection time budget exhausted"]
    assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == 1
    assert cli._run_history(conn)[(source, "near", "2", "RIX")]["succeeded"] is None
    assert called == (["first"] if source == "joinup" else [])


def test_freshness_is_per_source_tier_and_party_and_success_only(conn):
    add_run(conn, "joinup")
    add_run(conn, "waavo", errors=["HTTP 403"])
    add_run(conn, "waavo", "mid", pax="3")
    due = {task.key for task in cli.plan_collection(conn, ["2"], NOW)}
    assert ("joinup", "near", "2", "RIX") not in due
    assert ("waavo", "near", "2", "RIX") in due
    assert ("waavo", "mid", "2", "RIX") in due
    # A later failed task must not erase an earlier still-fresh success.
    add_run(conn, "joinup", started=NOW, errors=["partial: budget"])
    assert ("joinup", "near", "2", "RIX") not in {
        task.key for task in cli.plan_collection(conn, ["2"], NOW)}


def test_oldest_attempted_due_tasks_rotate_past_failed_near(conn):
    add_run(conn, "joinup", errors=["blocked"])
    add_run(conn, "waavo", errors=["partial: budget"])
    planned = cli.plan_collection(conn, ["2"], NOW)
    assert planned[0].tier == "mid"
    assert planned[2].tier == "far"
    assert {task.source for task in planned[:2]} == {"joinup", "waavo"}
    assert all(task.tier == "near" for task in planned[-2:])


def test_freshness_uses_completion_and_rejects_capped_or_abandoned_runs(conn):
    run_id = add_run(conn, started=NOW - timedelta(hours=5))
    conn.execute("UPDATE fetch_runs SET finished_at=:f WHERE id=:i",
                 {"f": NOW.isoformat(), "i": run_id})
    add_run(conn, "waavo", extra_params={"max_pages": 1})
    add_run(conn, "joinup", "mid", errors=["abandoned: no completion record"])
    due = {task.key for task in cli.plan_collection(conn, ["2"], NOW)}
    assert ("joinup", "near", "2", "RIX") not in due
    assert ("waavo", "near", "2", "RIX") in due
    assert ("joinup", "mid", "2", "RIX") in due


def test_unfinished_lease_does_not_return_false_success(conn):
    add_run(conn, finished=False)
    with pytest.raises(RuntimeError, match="blocked by unfinished"):
        cli._prepare_collection(conn, NOW)


def test_previous_github_job_is_recovered_even_when_recent(conn, monkeypatch):
    owner = {"GITHUB_RUN_ID": "100", "GITHUB_WORKFLOW": "collect",
             "GITHUB_REPOSITORY": "fixture/tour-finder"}
    run_id = add_run(conn, finished=False, owner=owner)
    for key, value in {**owner, "GITHUB_RUN_ID": "101"}.items():
        monkeypatch.setenv(key, value)
    assert cli._prepare_collection(conn, NOW) == 1
    row = conn.execute("SELECT finished_at, errors FROM fetch_runs WHERE id=:i",
                       {"i": run_id}).fetchone()
    assert row["finished_at"]
    assert "abandoned" in row["errors"]
    assert ("joinup", "near", "2", "RIX") in {
        task.key for task in cli.plan_collection(conn, ["2"], NOW)}


def test_different_workflow_cannot_reap_recent_owner(conn, monkeypatch):
    owner = {"GITHUB_RUN_ID": "100", "GITHUB_WORKFLOW": "manual collector",
             "GITHUB_REPOSITORY": "fixture/tour-finder"}
    add_run(conn, finished=False, owner=owner)
    for key, value in {**owner, "GITHUB_RUN_ID": "101", "GITHUB_WORKFLOW": "collect"}.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="blocked"):
        cli._prepare_collection(conn, NOW)


def test_github_rerun_recovers_previous_attempt_of_same_run(conn, monkeypatch):
    owner = {"GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
             "GITHUB_WORKFLOW": "collect", "GITHUB_REPOSITORY": "fixture/tour-finder"}
    add_run(conn, finished=False, owner=owner)
    for key, value in {**owner, "GITHUB_RUN_ATTEMPT": "2"}.items():
        monkeypatch.setenv(key, value)
    assert cli._prepare_collection(conn, NOW) == 1


def test_waavo_batch_reduces_sql_and_preserves_all_unique_prices(conn):
    statements = []
    event.listen(conn.engine, "before_cursor_execute",
                 lambda connection, cursor, statement, parameters, context, many:
                 statements.append(statement))
    result = fetcher.run_waavo_fetch(conn, WaavoFixture(
        [raw_waavo(index, reviews=True) for index in range(120)]), tier="near", pax_spec="2")
    assert result["completed"]
    assert result["offers_seen"] == 120
    # Old loop needed >= 600 SQL statements for these same 120 offers.
    assert len(statements) < 25
    assert conn.execute("SELECT count(*) FROM offers").scalar() == 120
    assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == 120
    assert conn.execute("SELECT count(*) FROM hotel_reviews").scalar() == 120


def test_duplicate_price_semantics_and_history_survive_batching(conn):
    rows = [raw_waavo(1, 100, link="https://example.invalid/first"),
            raw_waavo(1, 90, link="https://example.invalid/last")]
    first = fetcher.run_waavo_fetch(conn, WaavoFixture(rows))
    assert first["offers_seen"] == 1
    assert conn.execute("SELECT price_cents FROM price_snapshots").scalar() == 10000
    assert conn.execute("SELECT link FROM offers").scalar() == "https://example.invalid/last"
    second = fetcher.run_waavo_fetch(conn, WaavoFixture([raw_waavo(1, 80)]))
    assert second["offers_seen"] == 1
    assert conn.execute("SELECT count(*) FROM offers").scalar() == 1
    assert [r["price_cents"] for r in conn.execute(
        "SELECT price_cents FROM price_snapshots ORDER BY id").fetchall()] == [10000, 8000]


def test_duplicate_across_chunks_has_only_one_snapshot_per_run(conn):
    rows = [raw_waavo(index) for index in range(40)] + [raw_waavo(0, 70)]
    result = fetcher.run_waavo_fetch(conn, WaavoFixture(rows))
    assert result["offers_seen"] == 40
    assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == 40


def test_hot_pass_updates_same_snapshot_without_replacing_first_price(conn):
    class JoinUpFixture:
        lang = "lv"
        requests_made = 0

        def destinations(self, origin):
            return [{"id": "country"}]

        def stays(self, *args):
            return [7]

        def search_pages(self, *args, **kwargs):
            self.requests_made += 1
            price = 90 if kwargs.get("tour_types") else 100
            yield {"hotel": {"id": "1", "name": "Fixture"}, "offers": [{
                "from": {"id": "3164"}, "date_start": "2026-10-20",
                "stay": {"stay": 7}, "board": {"board_type": "AI"},
                "price": {"total_price": {"price": price}}}]}

    result = fetcher.run_fetch(conn, JoinUpFixture())
    assert result["completed"]
    row = conn.execute("SELECT price_cents, is_hot FROM price_snapshots").fetchone()
    assert row["price_cents"] == 10000 and row["is_hot"] == 1
    assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == 1


def test_deadline_keeps_observed_data_but_marks_task_incomplete(conn, monkeypatch):
    clock = [0]
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: clock[0])

    class BudgetFixture(WaavoFixture):
        def search_pages(self, *args, **kwargs):
            self.requests_made += 1
            yield raw_waavo(1)
            clock[0] = 11
            yield raw_waavo(2)

    result = fetcher.run_waavo_fetch(conn, BudgetFixture([]),
                                    tier="far", pax_spec="2", deadline=10)
    assert not result["completed"]
    assert "budget" in result["errors"][0]
    assert result["offers_seen"] == 1
    assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == 1
    row = conn.execute("SELECT finished_at, errors FROM fetch_runs").fetchone()
    assert row["finished_at"] and row["errors"]


def test_source_bootstrap_failure_is_recorded_not_left_running(conn):
    class BrokenFixture:
        requests_made = 1

        def destinations(self, origin):
            raise RuntimeError("fixture upstream failure")

    result = fetcher.run_fetch(conn, BrokenFixture())
    assert not result["completed"]
    assert conn.execute("SELECT finished_at FROM fetch_runs").scalar()


def test_batch_database_error_rolls_back_incomplete_chunk(conn):
    good = raw_waavo(1)
    bad = raw_waavo(2)
    bad["hotel"]["name"] = None  # DB NOT NULL failure inside the same batch.
    result = fetcher.run_waavo_fetch(conn, WaavoFixture([good, bad]))
    assert not result["completed"]
    assert conn.execute("SELECT count(*) FROM hotels").scalar() == 0
    assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == 0
    assert conn.execute("SELECT finished_at FROM fetch_runs").scalar()


def test_bounded_invocation_evaluates_subscriptions_without_pruning(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda path: conn)
    monkeypatch.setattr(conn, "close", Mock())
    evaluated = Mock(return_value=0)
    monkeypatch.setattr(subscriptions, "evaluate_all", evaluated)
    forbidden_prune = Mock(side_effect=AssertionError("must not delete historical data"))
    monkeypatch.setattr(fetcher, "prune_snapshots", forbidden_prune)

    def fake_fetch(connection, client, **kwargs):
        run_id = add_run(connection, tier=kwargs["tier"], pax=kwargs["pax_spec"],
                         started=datetime.now(timezone.utc))
        return {"run_id": run_id, "offers_seen": 1, "requests_made": 1, "errors": []}

    fake = Mock(side_effect=fake_fetch)
    monkeypatch.setattr(fetcher, "run_fetch", fake)
    args = SimpleNamespace(db="unused", pax=["2"], delay=0,
                           max_tasks=1, max_minutes=50)
    result = cli.cmd_collect(args)
    assert result == {"completed": 1, "attempted": 1, "pending": 5}
    assert fake.call_count == evaluated.call_count == 1
    forbidden_prune.assert_not_called()


def test_watchdog_checks_every_source_not_just_latest_snapshot(conn, monkeypatch):
    current = datetime.now(timezone.utc) - timedelta(minutes=1)
    for task in cli._collect_tasks(["2"]):
        add_run(conn, *task.key, started=current,
                errors=["upstream error"] if task.key == ("waavo", "far", "2", "RIX") else None)
    monkeypatch.setattr(db, "connect", lambda path: conn)
    monkeypatch.setattr(conn, "close", Mock())
    with pytest.raises(RuntimeError, match="waavo/far/2"):
        cli.cmd_assert_fresh(SimpleNamespace(db="unused", pax=["2"], hours=26))


def request_party(conn, spec, *, age_days=0):
    conn.execute("INSERT INTO pax_requests(spec,created_at) VALUES (:spec,:now)",
                 {"spec": spec, "now": (NOW - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    conn.commit()


def saved_party(conn, owner, adults, children_ages="", *, enabled=1, changes=None, raw=None):
    filters = {"date_from": "2026-10-09", "date_till": "2026-10-29",
               "adults": adults, "children_ages": children_ages, **(changes or {})}
    conn.execute("""INSERT INTO subscriptions(name,filters,enabled,created_at,owner_id)
        VALUES ('Fixture',:filters,:enabled,:now,:owner)""",
        {"filters": raw if raw is not None else json.dumps(filters), "enabled": enabled,
         "now": NOW.isoformat(), "owner": owner})
    conn.commit()


def test_all_recent_requested_parties_survive_newer_requests(conn, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    requested = ["1", "1+1:3", "1+1:4", "2+1:4", "2+1:5", "2+1:6"]
    for index, spec in enumerate(requested):
        request_party(conn, spec, age_days=6 - index)
    request_party(conn, "4", age_days=22)
    request_party(conn, "5", age_days=21)
    active = cli.active_pax_specs(conn, NOW)
    assert set(requested) <= set(active)
    assert "4" not in active and "5" in active
    assert active.index(requested[0]) < active.index(requested[-1])


def test_canonical_party_dedup_preserves_children_count_and_ignores_malformed_requests(conn, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    for spec in ["02+2:08,6", "2+2:6,8", "2+2:6,6", "2+0:", "7", "0", "2+2:7", "2+1:18", "2+2:6,,8", "bad"]:
        request_party(conn, spec)
    assert set(cli.active_pax_specs(conn, NOW)) == set(cli.DEFAULT_PAX) | {"2+2:6,8", "2+2:6,6"}


def test_only_active_unexpired_approved_owned_subscriptions_add_parties(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "100")
    for uid, status in [("200", "approved"), ("300", "denied"), ("400", "pending")]:
        conn.execute("""INSERT INTO telegram_access_requests(user_id,status,first_name,requested_at)
            VALUES (:id,:status,'Fixture',:now)""", {"id": uid, "status": status, "now": NOW.isoformat()})
    saved_party(conn, "100", 1)
    saved_party(conn, "200", 4, "8,6")
    saved_party(conn, "200", 4, [6, 8])
    for owner in ("300", "400", "500", None):
        saved_party(conn, owner, 5, "2")
    saved_party(conn, "200", 6, enabled=0)
    saved_party(conn, "100", 6, changes={"date_from": "2026-09-01", "date_till": "2026-10-07"})
    saved_party(conn, "200", 6, changes={"date_from": "2026-10-30", "date_till": "2026-10-20"})
    active = cli.active_pax_specs(conn, NOW)
    assert set(active) == set(cli.DEFAULT_PAX) | {"1", "4+2:6,8"}
    assert active.count("4+2:6,8") == 1
    conn.execute("UPDATE telegram_access_requests SET status='denied' WHERE user_id='200'")
    conn.commit()
    assert "4+2:6,8" not in cli.active_pax_specs(conn, NOW)


def test_malformed_legacy_filters_never_schedule_a_different_party(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "100")
    for raw in ["{bad", "[]", "null", "42", "{}"]:
        saved_party(conn, "100", 1, raw=raw)
    for adults, ages, changes in [(True, "", {}), (2.5, "", {}), (1, [False], {}),
                                  (1, "6,,8", {}), (1, "18", {}), (1, [1, 2, 3, 4, 5], {}),
                                  (1, "", {"date_till": "not-a-date"})]:
        saved_party(conn, "100", adults, ages, changes=changes)
    assert cli.active_pax_specs(conn, NOW) == cli.DEFAULT_PAX


def test_many_requested_parties_rotate_through_all_tasks_despite_failures(conn, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    for age in range(1, 7):
        request_party(conn, f"1+1:{age}")
    active = cli.active_pax_specs(conn, NOW)
    expected = {task.key for task in cli._collect_tasks(active)}
    attempted = set()
    for invocation in range((len(expected) + 5) // 6):
        planned = cli.plan_collection(conn, active, NOW + timedelta(minutes=invocation))[:6]
        for task in planned:
            assert task.key not in attempted
            attempted.add(task.key)
            add_run(conn, *task.key, started=NOW + timedelta(minutes=invocation), errors=["fixture upstream retry"])
    assert attempted == expected


def test_freshness_recognizes_canonical_legacy_child_order(conn):
    add_run(conn, pax="2+2:8,6")
    due = {task.key for task in cli.plan_collection(conn, ["2+2:6,8"], NOW)}
    assert ("joinup", "near", "2+2:6,8", "RIX") not in due
    assert ("waavo", "near", "2+2:6,8", "RIX") in due


def test_many_parties_still_obey_invocation_time_budget(conn, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    for age in range(1, 7):
        request_party(conn, f"1+1:{age}")
    monkeypatch.setattr(db, "connect", lambda path: conn)
    monkeypatch.setattr(conn, "close", Mock())
    clock = [0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    evaluated = Mock(return_value=0)
    monkeypatch.setattr(subscriptions, "evaluate_all", evaluated)

    def completed_at_deadline(connection, client, **kwargs):
        assert kwargs["deadline"] == 3000
        clock[0] = 3001
        run_id = add_run(connection, tier=kwargs["tier"], pax=kwargs["pax_spec"],
                         started=datetime.now(timezone.utc))
        return {"run_id": run_id, "offers_seen": 1, "requests_made": 1, "errors": []}

    fetch = Mock(side_effect=completed_at_deadline)
    monkeypatch.setattr(fetcher, "run_fetch", fetch)
    monkeypatch.setattr(fetcher, "run_waavo_fetch", fetch)
    result = cli.cmd_collect(SimpleNamespace(db="unused", pax=None, delay=0,
                                             max_tasks=6, max_minutes=50))
    assert result["attempted"] == result["completed"] == fetch.call_count == 1
    assert result["pending"] > 6
    evaluated.assert_called_once_with(conn, deadline=3000)


@pytest.mark.parametrize("malformed", ["source_list", "source_dict", "source_unknown", "naive_start", "naive_finish", "params_list"])
def test_malformed_history_does_not_crash_or_claim_fresh_coverage(conn, malformed):
    run_id = add_run(conn)
    if malformed.startswith("source_"):
        value = {"source_list": [], "source_dict": {}, "source_unknown": "unsupported"}[malformed]
        conn.execute("UPDATE fetch_runs SET params=:value WHERE id=:id", {"value": json.dumps({"source": value}), "id": run_id})
    elif malformed == "params_list":
        conn.execute("UPDATE fetch_runs SET params='[]' WHERE id=:id", {"id": run_id})
    else:
        column = "started_at" if malformed == "naive_start" else "finished_at"
        conn.execute(f"UPDATE fetch_runs SET {column}=:value WHERE id=:id", {"value": "2026-10-08T11:50:00", "id": run_id})
    conn.commit()
    assert len(cli.plan_collection(conn, ["2"], NOW)) == 6


@pytest.mark.parametrize("malformed", ["naive_start", "owner_list"])
def test_malformed_unfinished_lease_stays_protected(conn, malformed):
    run_id = add_run(conn, finished=False)
    if malformed == "naive_start":
        conn.execute("UPDATE fetch_runs SET started_at='2026-10-01T00:00:00' WHERE id=:id", {"id": run_id})
    else:
        conn.execute("UPDATE fetch_runs SET params=:value WHERE id=:id",
                     {"value": json.dumps({"collector_owner": ["malformed"]}), "id": run_id})
    conn.commit()
    with pytest.raises(RuntimeError, match="blocked by unfinished"):
        cli._prepare_collection(conn, NOW)
    assert conn.execute("SELECT finished_at FROM fetch_runs WHERE id=:id", {"id": run_id}).scalar() is None


def timed_run(conn, task, seconds, *, started=NOW - timedelta(days=2), **kwargs):
    run_id = add_run(conn, *task.key, started=started, **kwargs)
    if kwargs.get("finished", True):
        conn.execute("UPDATE fetch_runs SET finished_at=:finished WHERE id=:id",
                     {"finished": (started + timedelta(seconds=seconds)).isoformat(), "id": run_id})
        conn.commit()
    return run_id


def collect_task(source="joinup", tier="far", pax="2", origin="RIX"):
    _, first, last, hours = next(item for item in cli.TIERS if item[0] == tier)
    return cli.CollectTask(source, tier, pax, first, last, hours, origin)


def fake_timed_collector(conn, monkeypatch, tasks, durations, *, failures=(), evaluation_seconds=0):
    """Use the real due planner/run log with a clock, without network or sleeps."""
    clock, attempts = [0.0], []

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(seconds=clock[0])

    monkeypatch.setattr(cli, "datetime", ClockDatetime)
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli, "_collect_tasks", lambda specs: tasks)
    monkeypatch.setattr(db, "connect", lambda path: conn)
    monkeypatch.setattr(conn, "close", Mock())

    def fetch(source, connection, client, **kwargs):
        task = next(task for task in tasks if task.key == (source, kwargs["tier"], kwargs["pax_spec"], kwargs["origin"]))
        attempts.append(task.key)
        started = NOW + timedelta(seconds=clock[0])
        elapsed = durations[task.key]
        clock[0] += elapsed
        errors = ["fixture incomplete"] if task.key in failures else []
        run_id = timed_run(connection, task, elapsed, started=started, errors=errors)
        return {"run_id": run_id, "offers_seen": 1, "requests_made": 1, "errors": errors}

    monkeypatch.setattr(fetcher, "run_fetch", lambda *args, **kwargs: fetch("joinup", *args, **kwargs))
    monkeypatch.setattr(fetcher, "run_waavo_fetch", lambda *args, **kwargs: fetch("waavo", *args, **kwargs))

    def evaluate(*args, **kwargs):
        clock[0] += evaluation_seconds
        return 0

    evaluated = Mock(side_effect=evaluate)
    monkeypatch.setattr(subscriptions, "evaluate_all", evaluated)
    return clock, attempts, evaluated


def test_duration_estimate_prefers_latest_exact_over_faster_peers(conn):
    task = collect_task()
    timed_run(conn, task, 1800, started=NOW - timedelta(days=3))
    timed_run(conn, task, 1200, started=NOW - timedelta(days=2))
    timed_run(conn, collect_task(pax="3"), 60, started=NOW - timedelta(hours=2))
    assert cli._duration_estimate(task, cli._run_history(conn), NOW) == 1470


def test_duration_estimate_uses_only_three_latest_same_source_tier_peers(conn):
    for age, seconds in [(5, 9999), (4, 100), (3, 300), (2, 200)]:
        timed_run(conn, collect_task(pax="3"), seconds, started=NOW - timedelta(days=age))
    timed_run(conn, collect_task("waavo", pax="3"), 9999)
    timed_run(conn, collect_task(tier="near", pax="3"), 9999)
    assert cli._duration_estimate(collect_task(), cli._run_history(conn), NOW) == 270


@pytest.mark.parametrize("invalid", ["failed", "capped", "unfinished", "naive_start", "naive_finish",
                                     "negative", "old", "future", "malformed_errors"])
def test_unreliable_or_outdated_history_does_not_gate_unknown_task(conn, invalid):
    task = collect_task()
    kwargs = {"failed": {"errors": ["partial: budget"]},
              "capped": {"extra_params": {"max_pages": 1}},
              "unfinished": {"finished": False}}.get(invalid, {})
    started = NOW - timedelta(days=8) if invalid == "old" else NOW - timedelta(days=2)
    if invalid == "future":
        started = NOW + timedelta(minutes=1)
    run_id = timed_run(conn, task, -1 if invalid == "negative" else 600, started=started, **kwargs)
    if invalid.startswith("naive_"):
        column = "started_at" if invalid == "naive_start" else "finished_at"
        conn.execute(f"UPDATE fetch_runs SET {column}=:value WHERE id=:id",
                     {"value": started.replace(tzinfo=None).isoformat(), "id": run_id})
    elif invalid == "malformed_errors":
        conn.execute("UPDATE fetch_runs SET errors='{}' WHERE id=:id", {"id": run_id})
    conn.commit()
    assert cli._duration_estimate(task, cli._run_history(conn), NOW) is None


def test_old_exact_duration_falls_back_to_recent_peer(conn):
    task = collect_task()
    timed_run(conn, task, 9999, started=NOW - timedelta(days=8))
    timed_run(conn, collect_task(pax="3"), 100)
    assert cli._duration_estimate(task, cli._run_history(conn), NOW) == 150


def test_long_deferred_task_keeps_attempt_priority_and_runs_first_next_invocation(conn, monkeypatch, caplog):
    first, long, short = collect_task(tier="near"), collect_task(), collect_task("waavo")
    for task, age, seconds in [(first, 3, 100), (long, 2, 1200), (short, 1.1, 50)]:
        timed_run(conn, task, seconds, started=NOW - timedelta(days=age))
    before = cli._run_history(conn)[long.key]["attempted"]
    clock, attempted, evaluated = fake_timed_collector(
        conn, monkeypatch, [first, long, short], {first.key: 100, long.key: 1200, short.key: 50})
    args = SimpleNamespace(db="unused", pax=["2"], delay=0, max_tasks=2, max_minutes=10)
    with caplog.at_level("INFO", logger="tourfinder.collect"):
        result = cli.cmd_collect(args)
    assert result == {"completed": 2, "attempted": 2, "pending": 1}
    assert attempted == [first.key, short.key]
    assert evaluated.call_count == 2
    assert "deferred joinup/far/2" in caplog.text and "deferred=1" in caplog.text
    assert conn.execute("SELECT count(*) FROM fetch_runs").scalar() == 5
    assert cli._run_history(conn)[long.key]["attempted"] == before
    assert cli.plan_collection(conn, ["2"], NOW + timedelta(seconds=clock[0])) == [long]
    # The next invocation has a fresh full budget: the deferred long task
    # actually completes first instead of becoming another partial run.
    args.max_tasks = 1
    args.max_minutes = 50
    assert cli.cmd_collect(args) == {"completed": 1, "attempted": 1, "pending": 0}
    assert attempted == [first.key, short.key, long.key]


def test_first_task_always_attempts_even_if_previous_duration_exceeds_budget(conn, monkeypatch):
    task = collect_task()
    timed_run(conn, task, 1800)
    _, attempted, _ = fake_timed_collector(conn, monkeypatch, [task], {task.key: 30})
    assert cli.cmd_collect(SimpleNamespace(db="unused", pax=["2"], delay=0, max_tasks=6, max_minutes=1)) == {
        "completed": 1, "attempted": 1, "pending": 0}
    assert attempted == [task.key]


def test_all_remaining_estimates_too_long_ends_without_partial_attempt(conn, monkeypatch):
    first, long = collect_task("waavo", "near"), collect_task()
    timed_run(conn, long, 1200)
    _, attempted, _ = fake_timed_collector(conn, monkeypatch, [first, long], {first.key: 100, long.key: 1200})
    assert cli.cmd_collect(SimpleNamespace(db="unused", pax=["2"], delay=0, max_tasks=6, max_minutes=10)) == {
        "completed": 1, "attempted": 1, "pending": 1}
    assert attempted == [first.key]


def test_no_admission_deferral_until_one_task_completed(conn, monkeypatch):
    first, long = collect_task("waavo", "near"), collect_task()
    timed_run(conn, long, 1200)
    _, attempted, _ = fake_timed_collector(conn, monkeypatch, [first, long],
                                          {first.key: 100, long.key: 100}, failures=[first.key])
    with pytest.raises(RuntimeError, match="1 collection task"):
        cli.cmd_collect(SimpleNamespace(db="unused", pax=["2"], delay=0, max_tasks=6, max_minutes=10))
    assert attempted == [first.key, long.key]


def test_unknown_durations_still_attempt_and_preserve_six_attempt_limit(conn, monkeypatch):
    tasks = cli._collect_tasks(["2", "3"])
    # Distinct source/tier for the first six, so none has an observed peer yet.
    tasks.sort(key=lambda task: task.pax)
    _, attempted, _ = fake_timed_collector(conn, monkeypatch, tasks, {task.key: 1 for task in tasks})
    assert cli.cmd_collect(SimpleNamespace(db="unused", pax=["2", "3"], delay=0,
                                           max_tasks=6, max_minutes=1)) == {
        "completed": 6, "attempted": 6, "pending": 6}
    assert attempted == [task.key for task in tasks[:6]]


@pytest.mark.parametrize("evaluation_seconds,second_attempted", [(0, True), (15, False)])
def test_current_fetch_updates_peer_estimate_excluding_subscription_time(conn, monkeypatch,
                                                                       evaluation_seconds, second_attempted):
    first, second = collect_task(pax="2"), collect_task(pax="3")
    # No persisted timing history: 100s observed fetch => 150s admission estimate.
    # With 270s budget it fits after 100s fetch + 15s evaluation, whereas an
    # incorrectly combined 115s estimate (168s) would defer. A second 15s
    # evaluation adjustment below narrows the remaining window to 140s.
    _, attempted, _ = fake_timed_collector(conn, monkeypatch, [first, second],
                                          {first.key: 100, second.key: 100},
                                          evaluation_seconds=15 + evaluation_seconds)
    result = cli.cmd_collect(SimpleNamespace(db="unused", pax=["2", "3"], delay=0,
                                             max_tasks=6, max_minutes=4.5))
    assert attempted == ([first.key, second.key] if second_attempted else [first.key])
    assert result["attempted"] == (2 if second_attempted else 1)
