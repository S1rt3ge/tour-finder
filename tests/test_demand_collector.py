"""Bounded demand/collector seams use temporary SQLite and fake source IO."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from tourfinder import cli, collection_requests, db, demand, fetcher, subscriptions


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    engine = create_engine(f"sqlite:///{(tmp_path / 'collector.sqlite').as_posix()}")
    db.metadata.create_all(engine)
    conn = db.DB(engine)
    now = datetime.now(timezone.utc)
    filters = collection_requests.canonical_filters(dict(
        date_from=(now.date() + timedelta(days=1)).isoformat(),
        date_till=(now.date() + timedelta(days=21)).isoformat(),
        adults=1, origins="RIX,TLL,VNO", nights_min=5, nights_max=10,
        budget_max=1000, stars_min=4, board_categories="BB,HB,FB,AI,UAI"))
    conn.execute("""INSERT INTO subscriptions(name,filters,enabled,owner_id,created_at)
        VALUES ('private fixture',:filters,1,'123',:now)""", {"filters": json.dumps(filters), "now": now.isoformat()})
    conn.commit()
    original_close = conn.close
    monkeypatch.setattr(conn, "close", Mock())
    monkeypatch.setattr(db, "connect", lambda path: conn)
    evaluated = Mock(return_value=0)
    monkeypatch.setattr(subscriptions, "evaluate_all", evaluated)
    monkeypatch.setattr(fetcher, "run_fetch", Mock(side_effect=AssertionError("unexpected direct request")))
    monkeypatch.setattr(fetcher, "run_waavo_fetch", Mock(side_effect=AssertionError("unexpected legacy request")))
    yield conn, filters, evaluated
    original_close()
    engine.dispose()


def args(**kwargs):
    return SimpleNamespace(**(dict(db="unused", pax=None, delay=0, max_tasks=3, max_minutes=50) | kwargs))


class EmptyClient:
    requests_made = 0
    exhausted = True

    def search_pages(self, *args, **kwargs):
        self.requests_made += 1
        return iter(())


def test_actual_fetch_metadata_roundtrip_refreshes_only_exact_discovery(fixture):
    conn, filters, _ = fixture
    planned = demand.tasks_for(conn, [filters])
    task = planned[0]
    result = fetcher.run_waavo_demand_fetch(conn, EmptyClient(), filters=task.filters,
        query_key=task.query_key, pax_spec=task.pax, meal_group=task.meal_group,
        country_ids=task.country_ids, operator=task.operator, unsupported_reasons=task.unsupported_reasons)
    assert result["discovery_complete"] and not result["inventory_verified"]
    history = demand.run_history(conn)
    assert history[task.query_key]["succeeded"]
    assert task.query_key not in {task.query_key for task in demand.due_tasks(planned, history)}
    assert len(demand.due_tasks(planned, history)) == len(planned) - 1
    assert not any(row["succeeded"] for row in cli._run_history(conn).values())


def test_owned_mode_uses_bounded_waavo_before_direct_crawls_and_rotates_airports(fixture, monkeypatch):
    conn, filters, evaluated = fixture
    seen = []
    actual_fetch = fetcher.run_waavo_demand_fetch
    def fake(connection, client, **kwargs):
        assert client.deadline == kwargs["deadline"] <= cli.time.monotonic() + 180
        assert client.max_requests == kwargs["max_requests"] == 8
        assert kwargs["filters"]["budget_max"] == 1000
        assert kwargs["filters"]["stars_min"] == 4
        seen.append(kwargs["filters"]["origins"])
        return actual_fetch(connection, EmptyClient(), **kwargs)
    monkeypatch.setattr(fetcher, "run_waavo_demand_fetch", fake)
    result = cli.cmd_collect(args())
    assert result["attempted"] == result["completed"] == 3
    assert seen == ["RIX", "TLL", "VNO"]
    assert evaluated.call_count == 2  # first fast branch and final unseen rows
    assert all(task.tier == "demand" for task in cli._plan_collection_work(
        conn, cli.active_collection_scopes(conn), owned_filters=[filters]) if task.source == "waavo")


def test_successive_one_task_invocations_do_not_forget_recently_served_airport(fixture, monkeypatch):
    conn, _, _ = fixture
    seen = []
    actual = fetcher.run_waavo_demand_fetch
    def fake(connection, client, **kwargs):
        seen.append(kwargs["filters"]["origins"])
        return actual(connection, EmptyClient(), **kwargs)
    monkeypatch.setattr(fetcher, "run_waavo_demand_fetch", fake)
    for _ in range(3):
        assert cli.cmd_collect(args(max_tasks=1))["completed"] == 1
    assert seen == ["RIX", "TLL", "VNO"]


def test_direct_source_gets_a_turn_before_second_discovery_for_same_scope(fixture):
    conn, filters, _ = fixture
    tasks = cli._plan_collection_work(conn, cli.active_collection_scopes(conn), owned_filters=[filters])
    first_round = tasks[:6]
    assert {(task.source, task.origin) for task in first_round} == {
        (source, origin) for source in ("waavo", "joinup") for origin in ("RIX", "TLL", "VNO")}


def test_first_direct_crawl_not_starved_by_successful_discovery_and_padded_estimate(fixture, monkeypatch):
    conn, filters, _ = fixture
    filters |= {"origins": "RIX"}
    conn.execute("UPDATE subscriptions SET filters=:filters", {"filters": json.dumps(filters)})
    now = datetime.now(timezone.utc)
    finished = now - timedelta(days=2)
    for tier, *_ in cli.TIERS:
        conn.execute("""INSERT INTO fetch_runs(started_at,finished_at,tier,pax_spec,params,errors)
            VALUES (:start,:finish,:tier,'1',:params,'[]')""",
            {"start": (finished - timedelta(minutes=43)).isoformat(), "finish": finished.isoformat(),
             "tier": tier, "params": json.dumps({"source": "joinup", "origin": "RIX"})})
    conn.commit()
    history = cli._run_history(conn)
    assert all(cli._duration_estimate(task, history, now) > 50 * 60
               for task in cli._collect_tasks(["1"]) if task.source == "joinup")
    monkeypatch.setattr(cli.time, "monotonic", lambda: 100.0)
    order = []
    actual = fetcher.run_waavo_demand_fetch
    def discovery(connection, client, **kwargs):
        order.append("discovery")
        return actual(connection, EmptyClient(), **kwargs)
    def direct(connection, client, **kwargs):
        order.append("direct")
        assert kwargs["deadline"] == 3100.0  # Shared 50-minute deadline unchanged.
        return dict(run_id=999, offers_seen=0, requests_made=1, errors=[])
    direct_mock = Mock(side_effect=direct)
    monkeypatch.setattr(fetcher, "run_waavo_demand_fetch", discovery)
    monkeypatch.setattr(fetcher, "run_fetch", direct_mock)
    result = cli.cmd_collect(args(max_tasks=4))
    assert result["completed"] == result["attempted"] == 4
    assert order == ["discovery", "direct", "discovery", "discovery"]
    assert direct_mock.call_count == 1  # Later known oversized crawls defer.


def test_source_block_skips_all_other_waavo_but_direct_source_continues(fixture, monkeypatch):
    _, _, evaluated = fixture
    blocked = Mock(return_value=dict(run_id=1, offers_seen=0, requests_made=1,
                                    errors=["blocked"], source_blocked=True))
    direct = Mock(return_value=dict(run_id=2, offers_seen=0, requests_made=1, errors=[]))
    monkeypatch.setattr(fetcher, "run_waavo_demand_fetch", blocked)
    monkeypatch.setattr(fetcher, "run_fetch", direct)
    with pytest.raises(RuntimeError, match="1 collection task"):
        cli.cmd_collect(args())
    assert blocked.call_count == 1 and direct.call_count == 2
    assert evaluated.call_count == 3


def test_explicit_pax_keeps_legacy_override_even_with_owned_requests(fixture, monkeypatch):
    _, _, _ = fixture
    direct = Mock(return_value=dict(run_id=2, offers_seen=0, requests_made=1, errors=[]))
    legacy = Mock(return_value=dict(run_id=3, offers_seen=0, requests_made=1, errors=[]))
    bounded = Mock(side_effect=AssertionError("explicit override must not use discovery"))
    monkeypatch.setattr(fetcher, "run_fetch", direct)
    monkeypatch.setattr(fetcher, "run_waavo_fetch", legacy)
    monkeypatch.setattr(fetcher, "run_waavo_demand_fetch", bounded)
    assert cli.cmd_collect(args(pax=["2"], max_tasks=2))["completed"] == 2
    assert direct.call_args.kwargs["pax_spec"] == legacy.call_args.kwargs["pax_spec"] == "2"
    bounded.assert_not_called()


def test_diagnostic_unsupported_records_backoff_without_http_or_runtime_failure(fixture, monkeypatch):
    conn, filters, _ = fixture
    filters |= {"only_hot": True}
    conn.execute("UPDATE subscriptions SET filters=:filters", {"filters": json.dumps(filters)})
    conn.commit()
    class NoHTTP(EmptyClient):
        def search_pages(self, *args, **kwargs):
            raise AssertionError("unsupported must not call source")
    actual = fetcher.run_waavo_demand_fetch
    monkeypatch.setattr(fetcher, "run_waavo_demand_fetch", lambda connection, client, **kwargs: actual(connection, NoHTTP(), **kwargs))
    result = cli.cmd_collect(args(max_tasks=1))
    assert result["attempted"] == 1 and result["completed"] == 0
    history = demand.run_history(conn)
    assert len(history) == 1
    record = next(iter(history.values()))
    assert record["unsupported"] and not record["succeeded"]
    assert next(iter(history)) not in {task.query_key for task in demand.plan_demand(conn)}


def test_expired_global_deadline_stops_new_branch_and_keeps_first_results(fixture, monkeypatch):
    conn, _, evaluated = fixture
    clock = [100.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    actual = fetcher.run_waavo_demand_fetch
    def fake(connection, client, **kwargs):
        assert kwargs["deadline"] == 160.0  # outer budget tighter than 180 seconds
        result = actual(connection, EmptyClient(), **kwargs)
        clock[0] = 161.0
        return result
    mocked = Mock(side_effect=fake)
    monkeypatch.setattr(fetcher, "run_waavo_demand_fetch", mocked)
    assert cli.cmd_collect(args(max_tasks=10, max_minutes=1))["completed"] == 1
    assert mocked.call_count == 1 and evaluated.call_count == 1
    assert conn.execute("SELECT count(*) FROM fetch_runs WHERE finished_at IS NOT NULL").scalar() == 1


def test_watchdog_demands_exact_discovery_success_but_not_legacy_waavo_tiers(fixture, capsys):
    conn, filters, _ = fixture
    now = datetime.now(timezone.utc)
    for task in demand.tasks_for(conn, [filters]):
        fetcher.run_waavo_demand_fetch(conn, EmptyClient(), filters=task.filters,
            query_key=task.query_key, pax_spec=task.pax, meal_group=task.meal_group,
            country_ids=task.country_ids, operator=task.operator)
    for task in cli._collect_tasks(cli.active_collection_scopes(conn)):
        if task.source == "joinup":
            conn.execute("""INSERT INTO fetch_runs(started_at,finished_at,tier,pax_spec,params,errors)
                VALUES (:now,:now,:tier,:pax,:params,'[]')""",
                {"now": now.isoformat(), "tier": task.tier, "pax": task.pax,
                 "params": json.dumps({"source": "joinup", "origin": task.origin})})
    conn.commit()
    cli.cmd_assert_fresh(args(hours=26))
    output = capsys.readouterr().out
    assert "exact discovery branches" in output and "not full inventory" in output
    conn.execute("UPDATE fetch_runs SET errors='[\"partial\"]' WHERE id=(SELECT min(id) FROM fetch_runs)")
    conn.commit()
    with pytest.raises(RuntimeError, match="discovery/"):
        cli.cmd_assert_fresh(args(hours=26))
