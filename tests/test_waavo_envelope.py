"""HTTP-200 envelopes must not silently turn source failures into full runs."""
from copy import deepcopy
from datetime import date, timedelta
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from tourfinder import db, fetcher
from tourfinder.sources import waavo


def client_for(*bodies):
    responses = []
    for body in bodies:
        response = Mock(status_code=200)
        response.json.return_value = deepcopy(body)
        responses.append(response)
    session = Mock(headers={})
    session.get.side_effect = responses
    client = waavo.WaavoClient(delay=0, session=session)
    client._pause = Mock()  # No sleeps or network; _get still handles HTTP-200.
    return client


def read(client, **kwargs):
    return list(client.search_pages("2026-10-10", "2026-10-20", 2, **kwargs))


@pytest.mark.parametrize("body", [
    None, {}, [], "invalid", 1,
    {"error": "private upstream diagnostic must not appear in the exception"},
    {"data": None}, {"data": []}, {"data": "invalid"}, {"data": {}},
    {"data": {"offers": None}}, {"data": {"offers": {}}},
    {"data": {"offers": False}}, {"data": {"offers": ""}},
    {"data": {"offers": 0}},
])
def test_malformed_http_200_is_an_error_not_an_empty_search(body):
    client = client_for(body)
    with pytest.raises(waavo.WaavoError) as error:
        read(client)
    assert str(error.value) == "waavo_invalid_response_envelope"
    assert client.requests_made == client.session.get.call_count == 1


def test_valid_empty_list_is_a_normal_end():
    client = client_for({"data": {"offers": []}})
    assert read(client) == []
    assert client.requests_made == 1


def test_valid_short_page_preserves_its_offers_and_stops():
    offers = [{"fixture": 1}, {"fixture": 2}]
    client = client_for({"data": {"offers": offers}})
    assert read(client) == offers
    assert client.requests_made == 1


def test_full_page_then_valid_empty_list_uses_the_next_offset():
    offers = [{"fixture": i} for i in range(waavo.PAGE_SIZE)]
    client = client_for({"data": {"offers": offers}}, {"data": {"offers": []}})
    assert read(client) == offers
    assert [call.kwargs["params"]["offset"] for call in client.session.get.call_args_list] == [0, waavo.PAGE_SIZE]


def test_explicit_page_limit_still_stops_without_an_extra_request():
    offers = [{"fixture": i} for i in range(waavo.PAGE_SIZE)]
    client = client_for({"data": {"offers": offers}})
    assert read(client, max_pages=1) == offers
    assert client.requests_made == 1


def test_later_malformed_page_preserves_rows_but_run_is_incomplete(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'waavo-envelope.sqlite').as_posix()}")
    db.metadata.create_all(engine)
    conn = db.DB(engine)
    departure = (date.today() + timedelta(days=2)).isoformat()
    offers = [{"hotel": {"id": number, "name": "Fixture"}, "operator": {"code": "teztour"},
               "date": departure, "duration": 7,
               "pricing": {"price": 100, "currency": "EUR"}}
              for number in range(1, waavo.PAGE_SIZE + 1)]
    client = client_for({"data": {"offers": offers}}, {"error": "private diagnostic"})
    try:
        result = fetcher.run_waavo_fetch(conn, client, tier="near", pax_spec="2")
        assert result["errors"] == ["WaavoError"]
        assert result["offers_seen"] == waavo.PAGE_SIZE
        assert conn.execute("SELECT count(*) FROM offers").scalar() == waavo.PAGE_SIZE
        assert conn.execute("SELECT count(*) FROM price_snapshots").scalar() == waavo.PAGE_SIZE
        run = conn.execute("SELECT finished_at,errors FROM fetch_runs").fetchone()
        assert run["finished_at"] and json.loads(run["errors"]) == ["WaavoError"]
        assert client.requests_made == 2
    finally:
        conn.close()
        engine.dispose()
