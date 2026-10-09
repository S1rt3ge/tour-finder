"""Response-scope evidence uses isolated SQLite and fake pages, never a source."""
from datetime import date
import json

import pytest
from sqlalchemy import create_engine

from tourfinder import cli, db, fetcher
from tourfinder.sources.waavo import WaavoClient


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 9)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "date", FixedDate)
    engine = create_engine(f"sqlite:///{(tmp_path / 'scope.sqlite').as_posix()}")
    db.metadata.create_all(engine)
    connection = db.DB(engine)
    yield connection
    connection.close()
    engine.dispose()


def offer(number, **changes):
    return {"hotel": {"id": number, "name": "Fixture"}, "operator": {"code": "teztour"},
            "date": "2026-10-20", "duration": 7, "departureAirport": {"code": "RIX"},
            "adults": 1, "room": {"name": "Standard", "meal": {"group": {"code": "AI"}}},
            "pricing": {"price": 100, "currency": "EUR"}, **changes}


def client_with_pages(monkeypatch, pages):
    client = WaavoClient(delay=0)
    remaining = iter(pages)

    def get(**params):
        client.requests_made += 1
        return {"data": {"offers": next(remaining)}}

    monkeypatch.setattr(client, "_get", get)
    return client


def run(conn, client):
    return fetcher.run_waavo_fetch(conn, client, days_from=8, days_till=14,
                                   adults=1, tier="mid", pax_spec="1")


@pytest.mark.parametrize("changes,reason", [
    ({"date": "2026-10-16"}, "date_outside"),
    ({"date": "2026-10-24"}, "date_outside"),
    ({"date": "2027-06-19"}, "date_outside"),
    ({"date": "2026-02-30"}, "invalid_date"),
    ({"date": "20261020"}, "invalid_date"),
    ({"date": None}, "invalid_date"),
    ({"date": 20261020}, "invalid_date"),
    ({"duration": 1}, "nights_outside"),
    ({"duration": 22}, "nights_outside"),
    ({"duration": None}, "invalid_nights"),
    ({"duration": -1}, "invalid_nights"),
    ({"duration": True}, "invalid_nights"),
    ({"duration": 7.5}, "invalid_nights"),
    ({"duration": []}, "invalid_nights"),
])
def test_first_scope_violation_stops_and_cannot_refresh_task(conn, monkeypatch, changes, reason):
    # A full page would normally advance to another HTTP request. The first
    # offending row aborts before the third row or next page can be consumed.
    page = [offer(1), offer(2, **changes)] + [offer(index) for index in range(3, 101)]
    client = client_with_pages(monkeypatch, [page, [offer(101)]])
    result = run(conn, client)
    assert not result["completed"]
    assert result["errors"] == ["partial: waavo_response_scope_mismatch:" + reason]
    assert client.requests_made == 1
    assert conn.execute("SELECT count(*) FROM offers").scalar() == 1
    stored = conn.execute("SELECT source_hotel_id,date_start,nights FROM offers").fetchone()
    assert (stored["source_hotel_id"], stored["date_start"], stored["nights"]) == ("teztour:1", "2026-10-20", 7)
    assert cli._run_history(conn)[("waavo", "mid", "1", "RIX")]["succeeded"] is None
    params = json.loads(conn.execute("SELECT params FROM fetch_runs").scalar())
    assert params["scope_validation"] == "response_date_nights_v1"


def test_excluded_operator_still_exposes_ignored_scope_filter(conn, monkeypatch):
    client = client_with_pages(monkeypatch, [[offer(1, operator={"code": "joinup"}, date="2027-06-19")]])
    result = run(conn, client)
    assert not result["completed"] and len(result["errors"]) == 1
    assert conn.execute("SELECT count(*) FROM offers").scalar() == 0


def test_completed_earlier_pages_survive_later_scope_failure(conn, monkeypatch):
    client = client_with_pages(monkeypatch, [[offer(index) for index in range(100)],
                                            [offer(100, duration=30)], [offer(101)]])
    result = run(conn, client)
    assert not result["completed"] and result["offers_seen"] == 100
    assert client.requests_made == 2
    assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == 100


def test_valid_date_and_duration_boundaries_complete_without_relabelling(conn, monkeypatch):
    client = client_with_pages(monkeypatch, [[offer(1, date="2026-10-17", duration=2),
                                            offer(2, date="2026-10-23", duration=21),
                                            offer(3, duration=None, tripDuration=7)]])
    result = run(conn, client)
    assert result["completed"] and result["errors"] == []
    rows = conn.execute("SELECT date_start,nights FROM offers ORDER BY id").fetchall()
    assert [(row["date_start"], row["nights"]) for row in rows] == [
        ("2026-10-17", 2), ("2026-10-23", 21), ("2026-10-20", 7)]


def test_response_values_never_appear_in_scope_error_or_log(conn, monkeypatch, caplog):
    sensitive = "fixture-private-url-or-token"
    client = client_with_pages(monkeypatch, [[offer(1, date=sensitive)]])
    result = run(conn, client)
    assert result["errors"] == ["partial: waavo_response_scope_mismatch:invalid_date"]
    assert sensitive not in caplog.text
