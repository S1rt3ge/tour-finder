"""Admission/ownership regressions on disposable SQLite, without network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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
    monkeypatch.setattr(demand, "MAX_ACTIVE_ORIGIN_PARTIES", 4)
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
