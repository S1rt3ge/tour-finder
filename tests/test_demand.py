"""Exact bounded discovery planning; disposable SQLite, no source calls."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine

from tourfinder import collection_requests, db, demand

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
FILTERS = dict(date_from="2026-10-10", date_till="2026-10-25", adults=2,
               origins="RIX", nights_min=5, nights_max=10, budget_max=1000,
               board_categories="BB,HB,FB,AI,UAI", stars_min=4)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,456")
    engine = create_engine(f"sqlite:///{(tmp_path / 'demand.sqlite').as_posix()}")
    db.metadata.create_all(engine)
    connection = db.DB(engine)
    yield connection
    connection.close()
    engine.dispose()


def add_request(conn, filters=None, *, owner="123", expiry=None):
    filters = FILTERS if filters is None else filters
    conn.execute("""INSERT INTO collection_requests(owner_id,request_key,filters,created_at,updated_at,expires_at)
        VALUES (:owner,:key,:filters,:now,:now,:expiry)""",
        dict(owner=owner, key=str(conn.execute("SELECT count(*) FROM collection_requests").scalar()),
             filters=json.dumps(filters), now=collection_requests._iso(NOW),
             expiry=collection_requests._iso(expiry or NOW + timedelta(days=21))))
    conn.commit()


def add_subscription(conn, filters=None, *, owner="123", enabled=1):
    conn.execute("""INSERT INTO subscriptions(name,filters,owner_id,enabled,created_at)
        VALUES ('private fixture name',:filters,:owner,:enabled,:now)""",
        dict(filters=json.dumps(FILTERS if filters is None else filters), owner=owner,
             enabled=enabled, now=collection_requests._iso(NOW)))
    conn.commit()


def tasks(conn, filters=None, now=NOW):
    return demand.tasks_for(conn, [demand._filters(FILTERS if filters is None else filters)], now)


def add_run(conn, task, *, start=None, finish=True, complete=True, errors=None, params=None, tier="demand"):
    start = start or NOW - timedelta(minutes=2)
    values = dict(source="waavo", demand_contract=demand.DEMAND_CONTRACT,
        discovery_contract=demand.DISCOVERY_CONTRACT, query_key=task.query_key,
        filters=task.filters, meal_group=task.meal_group, country_ids=task.country_ids,
        operator=task.operator, discovery_complete=complete,
        unsupported_reasons=list(task.unsupported_reasons)) | (params or {})
    conn.execute("""INSERT INTO fetch_runs(started_at,finished_at,tier,pax_spec,params,errors)
        VALUES (:start,:finish,:tier,:pax,:params,:errors)""",
        dict(start=start.isoformat(), finish=(start + timedelta(seconds=30)).isoformat() if finish else None,
             tier=tier, pax=task.pax, params=json.dumps(values), errors=json.dumps(errors or [])))
    conn.commit()


def test_approved_requests_and_subscriptions_dedup_without_private_fields(conn):
    add_request(conn)
    add_subscription(conn, FILTERS | {"board_categories": "UAI,AI,FB,HB,BB"}, owner="456")
    add_subscription(conn, FILTERS | {"adults": 1}, owner="unapproved")
    add_subscription(conn, FILTERS | {"adults": 3}, enabled=0)
    values = demand.active_filters(conn, NOW)
    assert len(values) == 1 and values[0]["budget_max"] == 1000
    assert "owner" not in json.dumps(values) and "private fixture" not in json.dumps(values)


@pytest.mark.parametrize("changes", [
    {"date_till": "2026-10-09", "date_from": "2026-10-08"},
    {"date_from": "2026-11-24", "date_till": "2026-11-25"},
    {"date_from": "2026-10-25", "date_till": "2026-10-10"},
    {"adults": True}, {"children_ages": "99"}, {"origins": "XXX"},
    {"nights_min": 8, "nights_max": 7}, {"budget_max": -1}, {"stars_min": "4"},
    {"board_categories": "MADEUP"}, {"only_hot": "false"},
])
def test_invalid_or_uncollectable_legacy_filters_never_activate(conn, changes):
    add_request(conn, FILTERS | changes)
    add_subscription(conn, FILTERS | changes)
    assert demand.active_filters(conn, NOW) == []


def test_request_expiry_revocation_and_persisted_approval(conn, monkeypatch):
    add_request(conn, expiry=NOW)
    add_request(conn, owner="789")
    assert demand.active_filters(conn, NOW) == []
    conn.execute("INSERT INTO telegram_access_requests(user_id,status,requested_at) VALUES ('789','approved',:now)",
                 {"now": NOW.isoformat()})
    conn.commit()
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS")
    assert len(demand.active_filters(conn, NOW)) == 1
    conn.execute("UPDATE telegram_access_requests SET status='denied'")
    conn.commit()
    assert demand.active_filters(conn, NOW) == []


def test_strict_saved_two_adult_riga_filters_survive_meal_threshold(conn):
    planned = tasks(conn)
    assert planned and all(task.meal_group == "BB" for task in planned)
    assert {task.operator for task in planned} == set(demand.OPERATORS)
    for task in planned:
        assert task.filters["board_categories"] == "AI,BB,FB,HB,UAI"
        assert task.filters["budget_max"] == 1000 and task.filters["stars_min"] == 4
        assert (task.pax, task.origin, task.filters["nights_min"], task.filters["nights_max"]) == ("2", "RIX", 5, 10)
        assert task.tier == "demand" and task.max_requests == 8 and task.max_seconds == 180


@pytest.mark.parametrize("categories,boards,threshold,reason", [
    ("FB", None, "HB", None), ("UAI", None, "AI", None),
    ("BB,AI", "ALL", "AI", None), ("RO,AI", None, "RO", None),
    ("BB", "AI", None, "meal_filter_empty"),
    (None, "OT", None, "raw_board_unmapped"), ("OTHER", None, None, "meal_category_unsupported"),
])
def test_meal_and_semantics_are_preserved_without_unverified_threshold(conn, categories, boards, threshold, reason):
    planned = tasks(conn, FILTERS | {"board_categories": categories, "boards": boards})
    assert {task.meal_group for task in planned} == {threshold}
    assert all((reason in task.unsupported_reasons) if reason else not task.unsupported_reasons for task in planned)
    assert all(task.filters["boards"] == boards for task in planned)


@pytest.mark.parametrize("selected,expected,unsupported", [
    ("country:TR", ("15",), False), ("country:EG", ("18",), False),
    ("country:TR,country:EG", ("15", "18"), False),
    ("17", ("17",), False), ("18", None, True), ("c_8", None, True),
    ("999999999999999", None, True), ("country:UNVERIFIED", None, True),
])
def test_country_namespace_is_not_old_catalogue_namespace(conn, selected, expected, unsupported):
    planned = tasks(conn, FILTERS | {"countries": selected})
    assert {task.country_ids for task in planned} == {expected}
    assert all(("country_unmapped" in task.unsupported_reasons) == unsupported for task in planned)


def test_observed_raw_country_needs_same_id_and_unambiguous_country_name(conn):
    for identifier, name in (("5", "Portugāle"), ("500", "Turkey"), ("17", "Egypt")):
        conn.execute("INSERT INTO hotels(source,source_hotel_id,name,country_id,country_name) VALUES ('waavo',:id,'fixture',:id,:name)",
                     {"id": identifier, "name": name})
    conn.commit()
    assert tasks(conn, FILTERS | {"countries": "5"})[0].country_ids == ("5",)
    for identifier in ("500", "17"):
        assert "country_unmapped" in tasks(conn, FILTERS | {"countries": identifier})[0].unsupported_reasons


def test_date_parts_cover_supported_intersection_with_stable_interior_keys(conn):
    filters = FILTERS | {"date_from": "2026-10-01", "date_till": "2026-11-25", "nights_min": 1, "nights_max": 30,
                         "origins": "VNO,RIX,TLL", "adults": 1, "children_ages": [8, 6]}
    planned = tasks(conn, filters)
    assert {task.origin for task in planned} == {"RIX", "TLL", "VNO"}
    dates = set()
    for task in planned:
        first, last = map(date.fromisoformat, (task.filters["date_from"], task.filters["date_till"]))
        assert 0 <= (last - first).days < 7
        dates.update(first + timedelta(days=day) for day in range((last - first).days + 1))
        assert (task.filters["nights_min"], task.filters["nights_max"]) == (2, 21)
        assert task.pax == "1+2:6,8"
    assert min(dates) == date(2026, 10, 10) and max(dates) == date(2026, 11, 23) and len(dates) == 45
    tomorrow = tasks(conn, filters, NOW + timedelta(days=1))
    stable = {task.query_key for task in planned if task.filters["date_from"] >= "2026-10-12" and task.filters["date_till"] < "2026-11-23"}
    assert stable <= {task.query_key for task in tomorrow}


@pytest.mark.parametrize("changes,reason", [({"nights_min": 1, "nights_max": 1}, "nights_unsupported"),
    ({"nights_min": 22, "nights_max": 30}, "nights_unsupported"), ({"only_hot": True}, "hot_filter_unsupported")])
def test_unsupported_request_remains_diagnostic_task_without_broadening(conn, changes, reason):
    assert all(reason in task.unsupported_reasons for task in tasks(conn, FILTERS | changes))


@pytest.mark.parametrize("change", [{"budget_max": 999}, {"stars_min": 5}, {"boards": "BB"},
    {"board_categories": "BB"}, {"origins": "VNO"}, {"countries": "country:TR"},
    {"nights_min": 6}, {"nights_max": 11}, {"adults": 1}, {"children_ages": "7"},
    {"date_till": "2026-10-10"}, {"only_hot": True}])
def test_exact_filters_change_query_identity(conn, change):
    task = tasks(conn)[0]
    assert demand.query_key(task.filters | change, task.meal_group, task.country_ids, task.operator) != task.query_key


def test_owner_and_set_order_do_not_change_identity_but_operator_does(conn):
    first = tasks(conn)[0]
    reordered = first.filters | {"board_categories": "UAI,HB,FB,BB,AI", "owner_id": "private"}
    assert demand.query_key(reordered, first.meal_group, first.country_ids, first.operator) == first.query_key
    assert demand.query_key(first.filters, first.meal_group, first.country_ids, "different") != first.query_key


@pytest.mark.parametrize("params,finish,complete,errors", [
    ({"discovery_contract": "unknown"}, True, True, []), ({"filters": FILTERS}, True, True, []),
    ({"operator": "unknown"}, True, True, []), ({}, False, True, []),
    ({}, True, False, []), ({}, True, True, ["partial"]),
    ({"unsupported_reasons": ["unsupported"]}, True, True, []),
])
def test_non_exact_or_incomplete_run_cannot_refresh_branch(conn, params, finish, complete, errors):
    task = tasks(conn)[0]
    add_run(conn, task, params=params, finish=finish, complete=complete, errors=errors)
    assert demand.run_history(conn, NOW).get(task.query_key, {}).get("succeeded") is None


def test_completed_discovery_refreshes_only_exact_key_not_other_filters_or_legacy_tier(conn):
    from tourfinder import cli
    planned = tasks(conn)
    first = planned[0]
    add_run(conn, first)
    history = demand.run_history(conn, NOW)
    assert history[first.query_key]["succeeded"]
    due = demand.due_tasks(planned, history, NOW)
    assert first.query_key not in {task.query_key for task in due}
    assert len(due) == len(planned) - 1
    assert not any(record["succeeded"] for record in cli._run_history(conn).values())


def test_failure_and_unsupported_backoff_are_bounded_and_do_not_count_as_success(conn):
    failed, unsupported = tasks(conn)[:2]
    unsupported = replace(unsupported, unsupported_reasons=("country_unmapped",))
    add_run(conn, failed, complete=False, errors=["partial"])
    add_run(conn, unsupported, complete=False, errors=["partial"], start=NOW - timedelta(hours=1))
    history = demand.run_history(conn, NOW)
    assert demand.due_tasks([failed, unsupported], history, NOW) == []
    assert demand.due_tasks([failed, unsupported], history, NOW + timedelta(minutes=15)) == [failed]
    assert set(task.query_key for task in demand.due_tasks([failed, unsupported], history, NOW + timedelta(days=1))) == {failed.query_key, unsupported.query_key}


def test_fairness_remembers_successful_branch_filtered_out_of_due_list(conn):
    planned = tasks(conn, FILTERS | {"origins": "RIX,TLL,VNO"})
    seen = []
    for attempt in range(3):
        chosen = demand.due_tasks(planned, demand.run_history(conn, NOW), NOW)[0]
        seen.append(chosen.origin)
        add_run(conn, chosen, start=NOW - timedelta(minutes=3) + timedelta(seconds=attempt))
    assert seen == ["RIX", "TLL", "VNO"]
    first_round = demand.due_tasks(planned, demand.run_history(conn, NOW), NOW)[:3]
    assert len({task.origin for task in first_round}) == 3


@pytest.mark.parametrize("started", ["2026-10-09T11:00:00", "2027-10-09T11:00:00Z", "bad"])
def test_invalid_or_future_run_time_does_not_affect_rotation(conn, started):
    task = tasks(conn)[0]
    add_run(conn, task)
    conn.execute("UPDATE fetch_runs SET started_at=:value", {"value": started})
    conn.commit()
    assert demand.run_history(conn, NOW) == {}
