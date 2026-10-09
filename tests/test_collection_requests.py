"""Admission/ownership regressions on disposable SQLite, without network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from threading import Barrier

import pytest
from sqlalchemy import create_engine

from tourfinder import collection_requests as demand, db

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
FILTERS = dict(date_from="2026-10-10", date_till="2026-10-25", adults=1,
               origins="RIX", nights_min=5, nights_max=10)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,456")
    engine = create_engine(f"sqlite:///{(tmp_path / 'demand.sqlite').as_posix()}")
    db.metadata.create_all(engine)
    connection = db.DB(engine)
    yield connection
    connection.close()
    engine.dispose()


def enqueue(conn, owner="123", filters=None, now=NOW):
    try:
        row = demand.queue_request(conn, owner, filters or FILTERS, now=now)
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise


def test_canonical_idempotency_and_cooldown_do_not_lose_matching_constraints(conn):
    filters = FILTERS | dict(origins="VNO,RIX", boards="AI,BB", children_ages="7,5", stars_min=4)
    first = enqueue(conn, filters=filters)
    again = enqueue(conn, filters=filters | dict(origins="RIX,VNO", boards="BB,AI", children_ages="5,7"), now=NOW + timedelta(minutes=1))
    assert first == again
    refreshed = enqueue(conn, filters=filters, now=NOW + timedelta(minutes=16))
    assert refreshed["id"] == first["id"] and refreshed["created_at"] == first["created_at"]
    assert refreshed["updated_at"] > first["updated_at"]
    assert demand.request_key(filters | {"sort": "price_per_night"}) == demand.request_key(filters)
    for field, value in (("origins", "TLL"), ("date_till", "2026-10-26"), ("nights_max", 11), ("stars_min", 5), ("budget_max", 1000)):
        assert demand.request_key(filters | {field: value}) != demand.request_key(filters)


def test_requests_and_matching_lookups_are_owner_scoped(conn):
    first = enqueue(conn)
    assert demand.matching_request(conn, "456", FILTERS, NOW) is None
    second = enqueue(conn, owner="456")
    assert first["id"] != second["id"]
    assert demand.matching_request(conn, "123", FILTERS, NOW)["id"] == first["id"]
    assert demand.matching_request(conn, "456", FILTERS, NOW)["id"] == second["id"]


def test_owner_limit_allows_idempotent_repeat_and_other_owner(conn, monkeypatch):
    monkeypatch.setattr(demand, "MAX_OWNER_REQUESTS", 2)
    first = enqueue(conn)
    enqueue(conn, filters=FILTERS | {"stars_min": 4})
    with pytest.raises(demand.RequestLimit, match="активных заявок"):
        enqueue(conn, filters=FILTERS | {"stars_min": 5})
    assert enqueue(conn)["id"] == first["id"]
    assert enqueue(conn, owner="456")["owner_id"] == "456"


def test_global_capacity_counts_distinct_origin_party_not_filter_variations(conn, monkeypatch):
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 1)
    enqueue(conn)
    enqueue(conn, owner="456", filters=FILTERS | {"stars_min": 4})
    with pytest.raises(demand.RequestLimit, match="Очередь"):
        enqueue(conn, filters=FILTERS | {"origins": "VNO"})
    assert len(demand.active_scopes(conn, NOW)) == 2


def test_revocation_expiry_and_outside_horizon_do_not_activate_work(conn, monkeypatch):
    enqueue(conn)
    assert demand.active_scopes(conn, NOW)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "456")
    assert demand.active_scopes(conn, NOW) == []
    with pytest.raises(demand.RequestLimit, match="одобрен"):
        enqueue(conn)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,456")
    assert demand.active_scopes(conn, NOW + timedelta(days=22)) == []
    assert demand.matching_request(conn, "123", FILTERS, NOW + timedelta(days=22)) is None
    conn.execute("UPDATE collection_requests SET expires_at='2027-01-01T00:00:00Z',filters=:filters",
                 {"filters": '{"date_from":"2027-03-01","date_till":"2027-03-10","origins":"VNO"}'})
    conn.commit()
    assert demand.active_scopes(conn, NOW) == []


def test_concurrent_admission_cannot_overbook_owner_quota(conn, monkeypatch):
    monkeypatch.setattr(demand, "MAX_OWNER_REQUESTS", 1)
    barrier = Barrier(2)

    def submit(stars):
        other = db.DB(conn.engine)
        try:
            barrier.wait(timeout=5)
            try:
                enqueue(other, filters=FILTERS | {"stars_min": stars})
                return "accepted"
            except demand.RequestLimit:
                return "limited"
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (4, 5)))
    assert sorted(results) == ["accepted", "limited"]
    assert conn.execute("SELECT count(*) FROM collection_requests").scalar() == 1


def add_subscription(conn, filters=None, *, owner="123", enabled=1):
    conn.execute("""INSERT INTO subscriptions(name,filters,enabled,created_at,owner_id)
        VALUES ('fixture',:filters,:enabled,:now,:owner)""",
        {"filters": json.dumps(FILTERS if filters is None else filters), "enabled": enabled,
         "now": demand._iso(NOW), "owner": owner})
    conn.commit()


def future_filters(**changes):
    return FILTERS | {"date_from": "2027-03-01", "date_till": "2027-03-10"} | changes


def admit(conn, filters, now=NOW):
    try:
        demand.admit_scope(conn, "123", filters, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def test_first_owned_baltic_request_replaces_bootstrap_without_deleting_it(conn, monkeypatch):
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 3)
    conn.execute("INSERT INTO pax_requests(spec,created_at) VALUES ('5',:now)", {"now": demand._iso(NOW)})
    conn.commit()
    row = enqueue(conn, filters=FILTERS | {"origins": "VNO,RIX,TLL"})
    assert demand.matching_request(conn, "123", FILTERS | {"origins": "RIX,TLL,VNO"}, NOW)["id"] == row["id"]
    assert conn.execute("SELECT spec FROM pax_requests").scalar() == "5"
    with pytest.raises(demand.RequestLimit, match="Очередь"):
        enqueue(conn, filters=FILTERS | {"adults": 4})
    assert conn.execute("SELECT count(*) FROM collection_requests").scalar() == 1


def test_all_current_owned_scopes_and_subscription_are_preserved(conn, monkeypatch):
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 4)
    add_subscription(conn, FILTERS | {"adults": 2})
    first = enqueue(conn, filters=FILTERS | {"origins": "VNO,RIX,TLL"})
    assert enqueue(conn, owner="456", filters=FILTERS | {"origins": "TLL"})
    with pytest.raises(demand.RequestLimit, match="Очередь"):
        enqueue(conn, owner="456", filters=FILTERS | {"adults": 3})
    assert demand.matching_request(conn, "123", FILTERS | {"origins": "RIX,TLL,VNO"}, NOW)["id"] == first["id"]
    assert conn.execute("SELECT count(*) FROM collection_requests").scalar() == 2
    assert conn.execute("SELECT count(*) FROM subscriptions WHERE enabled=1").scalar() == 1


def test_future_only_subscription_reserves_capacity_without_suppressing_bootstrap(conn, monkeypatch):
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 4)
    add_subscription(conn, future_filters(origins="VNO"))
    with pytest.raises(demand.RequestLimit, match="Очередь"):
        admit(conn, future_filters(origins="TLL"))
    # Entering current owned demand replaces bootstrap, not the future reserve.
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 2)
    enqueue(conn)
    with pytest.raises(demand.RequestLimit, match="Очередь"):
        enqueue(conn, filters=FILTERS | {"origins": "TLL"})
    assert conn.execute("SELECT count(*) FROM subscriptions").scalar() == 1


def test_new_future_subscription_cannot_bypass_capacity_with_current_owned_work(conn, monkeypatch):
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 1)
    enqueue(conn)
    admit(conn, future_filters())  # same party/airport is shared
    with pytest.raises(demand.RequestLimit, match="Очередь"):
        admit(conn, future_filters(origins="VNO"))
    assert conn.execute("SELECT count(*) FROM collection_requests").scalar() == 1


@pytest.mark.parametrize("changes,owner,enabled,accepted", [
    ({}, "123", 1, True),
    ({"date_from": "2026-11-23", "date_till": "2026-11-23"}, "123", 1, True),
    ({"date_from": "2026-11-24", "date_till": "2026-11-24"}, "123", 1, False),
    ({"date_from": "2026-10-09", "date_till": "2026-10-09"}, "123", 1, False),
    ({"date_from": "2026-10-30", "date_till": "2026-10-25"}, "123", 1, False),
    ({}, "789", 1, False),
    ({}, "123", 0, False),
    ({"adults": True}, "123", 1, False),
    ({"adults": 1.5}, "123", 1, False),
    ({"children_ages": "5,5,5,5,5"}, "123", 1, False),
    ({"origins": "LHR"}, "123", 1, False),
])
def test_only_valid_current_approved_enabled_subscription_suppresses_bootstrap(
        conn, monkeypatch, changes, owner, enabled, accepted):
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 2)
    add_subscription(conn, FILTERS | changes, owner=owner, enabled=enabled)
    if accepted:
        admit(conn, future_filters(origins="VNO"))
    else:
        with pytest.raises(demand.RequestLimit, match="Очередь"):
            admit(conn, future_filters(origins="VNO"))


def test_legacy_valid_subscription_children_list_matches_collector_scope(conn, monkeypatch):
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 1)
    add_subscription(conn, FILTERS | {"children_ages": [7, 5]})
    admit(conn, future_filters(children_ages="5,7"))


@pytest.mark.parametrize("invalidate", ["expired", "revoked", "past", "reversed", "invalid_party"])
def test_inactive_owned_request_does_not_disable_bootstrap(conn, monkeypatch, invalidate):
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 1)
    row = enqueue(conn)
    if invalidate == "expired":
        conn.execute("UPDATE collection_requests SET expires_at=:at", {"at": demand._iso(NOW - timedelta(seconds=1))})
    elif invalidate == "revoked":
        conn.execute("UPDATE collection_requests SET owner_id='789'")
    else:
        changes = ({"date_from": "2026-10-08", "date_till": "2026-10-09"} if invalidate == "past"
                   else {"date_from": "2026-10-30", "date_till": "2026-10-10"} if invalidate == "reversed"
                   else {"adults": False})
        conn.execute("UPDATE collection_requests SET filters=:filters", {"filters": json.dumps(FILTERS | changes)})
    conn.commit()
    with pytest.raises(demand.RequestLimit, match="Очередь"):
        admit(conn, future_filters())
    assert conn.execute("SELECT id FROM collection_requests").scalar() == row["id"]


@pytest.mark.parametrize("spec", ["1", "1+0:", " 01 ", "1+2:7, 5"])
def test_bootstrap_counts_recent_valid_legacy_variants(conn, monkeypatch, spec):
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 4)
    conn.execute("INSERT INTO pax_requests(spec,created_at) VALUES (:spec,:now)",
                 {"spec": spec, "now": demand._iso(NOW)})
    conn.commit()
    with pytest.raises(demand.RequestLimit, match="Очередь"):
        admit(conn, future_filters(origins="VNO"))


@pytest.mark.parametrize("spec,at", [
    ("1+2:7", NOW), ("0", NOW), ("1+1:18", NOW),
    ("1", NOW + timedelta(seconds=1)), ("1", NOW - timedelta(days=21, seconds=1)),
])
def test_invalid_or_inactive_legacy_does_not_reserve_bootstrap_scope(conn, monkeypatch, spec, at):
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 4)
    conn.execute("INSERT INTO pax_requests(spec,created_at) VALUES (:spec,:at)",
                 {"spec": spec, "at": demand._iso(at)})
    conn.commit()
    admit(conn, future_filters(origins="VNO"))


def test_concurrent_first_owned_scopes_do_not_overbook_global_capacity(conn, monkeypatch):
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 1)
    barrier = Barrier(2)

    def submit(item):
        owner, origin = item
        other = db.DB(conn.engine)
        try:
            barrier.wait(timeout=5)
            try:
                enqueue(other, owner=owner, filters=FILTERS | {"origins": origin})
                return "accepted"
            except demand.RequestLimit:
                return "limited"
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (("123", "RIX"), ("456", "VNO"))))
    assert sorted(results) == ["accepted", "limited"]
    assert conn.execute("SELECT count(*) FROM collection_requests").scalar() == 1
