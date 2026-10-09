"""Exact discovery persistence on isolated SQLite and fake source pages."""
import json

import pytest
from sqlalchemy import create_engine

from tourfinder import db, fetcher, queries
from tourfinder.sources import waavo


@pytest.fixture
def conn(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'demand-fetch.sqlite').as_posix()}")
    db.metadata.create_all(engine)
    connection = db.DB(engine)
    yield connection
    connection.close()
    engine.dispose()


def filters(**changes):
    return dict(date_from="2026-10-17", date_till="2026-10-23", adults=1,
                children_ages=None, origins="RIX", nights_min=5, nights_max=10,
                budget_max=1000, board_categories="BB,HB,FB,AI,UAI", stars_min=4, **changes)


def row(number=1, **changes):
    return {"offerKey": f"{number:032x}", "hotelId": number, "hotelName": "Fixture",
            "hotelRating": 4, "countryId": 15, "countryName": "Turkey", "cityName": "Antalya",
            "operatorCode": "novaturas", "date": "2026-10-20", "duration": 7,
            "tripDuration": 7, "departureAirportCode": "RIX", "departureAirport": "Riga Intl",
            "adults": 1, "children": 0, "childrenAge": [], "price": 750, "currency": "EUR",
            "mealGroupCode": "BB", "mealTranslation": "Breakfast", "roomName": "Standard",
            "link": "https://astrature.lv/fixture", **changes}


class FakeClient:
    def __init__(self, rows=(), error=None, exhausted=True):
        self.rows, self.error, self.finish_exhausted = rows, error, exhausted
        self.requests_made, self.exhausted, self.calls = 0, False, []

    def search_pages(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.requests_made += 1
        yield from self.rows
        if self.error:
            raise self.error
        self.exhausted = self.finish_exhausted


def run(conn, client, selected=None, **kwargs):
    return fetcher.run_waavo_demand_fetch(conn, client, filters=selected or filters(),
            query_key="a" * 64, pax_spec="1", meal_group="BB", **kwargs)


def test_bounded_query_passes_supported_filters_and_keeps_real_identity(conn):
    client = FakeClient([row()])
    selected = filters(countries="country:TR")
    result = run(conn, client, selected, country_ids=("15", "18"))
    assert result["completed"] and result["discovery_complete"] and not result["inventory_verified"]
    args, sent = client.calls[0]
    assert args == ("2026-10-17", "2026-10-23", 1)
    assert sent == dict(children_ages=[], duration_from=5, duration_till=10, origin="RIX",
                        meal_group="BB", stars_min=4, country_ids=("15", "18"), budget_max=1000,
                        operator=None)
    saved = dict(conn.execute("SELECT * FROM offers").fetchone())
    assert saved["origin_id"] == "RIX" and saved["date_start"] == "2026-10-20"
    assert saved["room_code"].startswith("wu2:")
    assert saved["pax_adl"] == 1 and saved["pax_chd"] == 0
    metadata = json.loads(conn.execute("SELECT params FROM fetch_runs").scalar())
    assert metadata["discovery_complete"] and metadata["query_key"] == "a" * 64
    assert metadata["demand_contract"] == "waavo_discovery_v1"
    assert "inventory_contract" not in metadata
    assert conn.execute("SELECT tier FROM fetch_runs").scalar() == "demand"


@pytest.mark.parametrize("changes", [{"price": 1001}, {"hotelRating": 3}, {"mealGroupCode": "RO"}])
def test_local_filter_rejects_upstream_superset(conn, changes):
    result = run(conn, FakeClient([row(), row(2, **changes)]))
    assert result["discovery_complete"] and result["local_filter_rejections"] == 1
    assert conn.execute("SELECT count(*) FROM offers").scalar() == 1


def test_raw_boards_and_categories_are_intersection_and_countries_remain_exact(conn):
    selected = filters(boards="AI", countries="country:TR")
    result = run(conn, FakeClient([row(1, mealGroupCode="AI"), row(2),
            row(3, mealGroupCode="AI", countryId=28, countryName="Greece")]), selected)
    assert result["local_filter_rejections"] == 2 and result["offers_seen"] == 1


@pytest.mark.parametrize("changes", [{"date": "2027-01-01"}, {"duration": 11},
    {"adults": 2}, {"departureAirportCode": "VNO"}, {"children": 1, "childrenAge": [7]},
    {"currency": None}, {"currency": "USD"}])
def test_bad_scope_or_missing_echo_stops_without_poisoning_valid_rows(conn, changes):
    client = FakeClient([row(), row(2, **changes), row(3)])
    result = run(conn, client)
    assert not result["completed"] and not result["discovery_complete"]
    assert conn.execute("SELECT count(*) FROM offers").scalar() == 1
    assert conn.execute("SELECT finished_at FROM fetch_runs").scalar()


@pytest.mark.parametrize("error,blocked", [(waavo.WaavoDeadlineError("fixture budget"), False),
                                           (waavo.WaavoBlockedError("fixture block"), True)])
def test_partial_source_failure_preserves_observations_and_bounded_diagnostic(conn, error, blocked):
    result = run(conn, FakeClient([row()], error=error))
    assert not result["completed"] and result["source_blocked"] is blocked
    assert result["offers_seen"] == 1
    assert "fixture" not in json.dumps(result)
    assert not json.loads(conn.execute("SELECT params FROM fetch_runs").scalar())["discovery_complete"]


def test_generator_return_without_empty_page_does_not_complete(conn):
    result = run(conn, FakeClient([row()], exhausted=False))
    assert result["errors"] == ["partial: discovery_not_exhausted"]
    assert not result["discovery_complete"] and result["offers_seen"] == 1


def test_empty_discovery_can_complete_only_its_query(conn):
    result = run(conn, FakeClient())
    assert result["discovery_complete"] and result["offers_seen"] == 0
    assert result["inventory_verified"] is False


def test_unsupported_branch_records_attempt_without_network(conn):
    client = FakeClient([row()])
    result = run(conn, client, unsupported_reasons=("unmapped_country",))
    assert not client.calls and result["requests_made"] == 0
    assert not result["completed"] and not result["discovery_complete"]


def test_unsupported_nights_still_record_diagnostic_for_backoff(conn):
    selected = filters()
    selected.update(nights_min=22, nights_max=30)
    client = FakeClient([row()])
    result = run(conn, client, selected, unsupported_reasons=("nights_outside_source_range",))
    assert not client.calls and not result["completed"]
    params = json.loads(conn.execute("SELECT params FROM fetch_runs").scalar())
    assert params["unsupported_reasons"] == ["nights_outside_source_range"]
    assert params["meal_group"] == "BB"


def test_cheap_endpoint_does_not_invent_hot_tour_flag(conn):
    selected = filters(only_hot=True)
    result = run(conn, FakeClient([row()]), selected)
    assert result["offers_seen"] == 0


def test_operator_scope_mismatch_stops_before_storing_wrong_operator(conn):
    result = run(conn, FakeClient([row(), row(2, operatorCode="coral"), row(3)]), operator="novaturas")
    assert result["errors"] == ["partial: waavo_response_scope_mismatch:operator"]
    assert result["offers_seen"] == 1 and not result["discovery_complete"]


def test_stored_matching_observation_is_searchable_with_same_filters(conn):
    selected = filters(countries="country:TR")
    run(conn, FakeClient([row()]), selected)
    assert len(queries.search_offers(conn, **selected)) == 1
