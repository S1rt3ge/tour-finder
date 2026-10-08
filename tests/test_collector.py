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


def add_run(conn, source="joinup", tier="near", pax="2", *, started=None,
            finished=True, errors=None, owner=None, extra_params=None):
    started = started or NOW - timedelta(minutes=10)
    params = {"source": source, **(extra_params or {})}
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


def test_freshness_is_per_source_tier_and_party_and_success_only(conn):
    add_run(conn, "joinup")
    add_run(conn, "waavo", errors=["HTTP 403"])
    add_run(conn, "waavo", "mid", pax="3")
    due = {task.key for task in cli.plan_collection(conn, ["2"], NOW)}
    assert ("joinup", "near", "2") not in due
    assert ("waavo", "near", "2") in due
    assert ("waavo", "mid", "2") in due
    # A later failed task must not erase an earlier still-fresh success.
    add_run(conn, "joinup", started=NOW, errors=["partial: budget"])
    assert ("joinup", "near", "2") not in {
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
    assert ("joinup", "near", "2") not in due
    assert ("waavo", "near", "2") in due
    assert ("joinup", "mid", "2") in due


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
    assert ("joinup", "near", "2") in {
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
                errors=["upstream error"] if task.key == ("waavo", "far", "2") else None)
    monkeypatch.setattr(db, "connect", lambda path: conn)
    monkeypatch.setattr(conn, "close", Mock())
    with pytest.raises(RuntimeError, match="waavo/far/2"):
        cli.cmd_assert_fresh(SimpleNamespace(db="unused", pax=["2"], hours=26))
