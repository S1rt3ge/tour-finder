"""Discovery protocol and normalization tests: fake HTTP only, no database."""
from copy import deepcopy
import json

import pytest
import requests

from tourfinder.sources import waavo, waavo_discovery as discovery


class Response:
    def __init__(self, data=None, status=200, body=None):
        self.status_code = status
        self.body = json.dumps(data).encode() if body is None else body
        self.closed = False

    def iter_content(self, chunk_size):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index + chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers, self.calls = {}, []

    def get(self, url, **kwargs):
        return self.request("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, kwargs)

    def request(self, method, url, kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def row(number, **changes):
    return {"hotelId": number, "hotelName": "Fixture hotel", "hotelRating": 4,
            "offerKey": f"{number:032x}", "operatorCode": "novaturas",
            "adults": 1, "children": 0, "childrenAge": [],
            "departureAirportCode": "VNO", "departureAirport": "Vilnius",
            "date": "2026-10-20", "duration": 7, "tripDuration": 8,
            "roomName": "Standard", "mealGroupCode": "AI", "mealTranslation": "All Inclusive",
            "countryId": 15, "countryName": "Turkey", "cityName": "Side",
            "price": 499, "pricePerPerson": 499, "priceBefore": 550, "currency": "EUR",
            "transferIncluded": True, "tripadvisorRating": 4.2, "tripadvisorRatingsCount": 32,
            "images": ["https://example.invalid/image.jpg"],
            "link": "https://example.invalid/real-source-link", **changes}


def make_client(pages, **kwargs):
    responses = [page if isinstance(page, (Response, Exception)) else Response({"results": page, "request": []})
                 for page in pages]
    session = Session(responses)
    return discovery.WaavoDiscoveryClient(delay=0, session=session, **kwargs), session


def search(client, **kwargs):
    return client.search_pages("2026-10-17", "2026-10-23", 1, origin="VNO", **kwargs)


def test_exact_get_filters_and_post_cursor_use_raw_counts_until_empty():
    client, session = make_client([[row(1), row(2, operatorCode="joinup", price=600)], [row(3, price=700)], []])
    rows = list(search(client, children_ages=[8, 6], duration_from=5, duration_till=9,
                       meal_group="AI", stars_min=4, country_ids=[18, "15", 18],
                       budget_max=1000, operator="novaturas", tripadvisor_rating_min=4))
    assert len(rows) == 3 and client.exhausted
    first, second, third = session.calls
    assert [call[0] for call in session.calls] == ["GET", "POST", "POST"]
    assert all(call[1] == discovery.BASE_URL and call[2]["allow_redirects"] is False for call in session.calls)
    assert first[2]["params"] == {
        "departureAirport[]": ["VNO"], "dateFrom": "2026-10-17", "dateTo": "2026-10-23",
        "adults": 1, "children": 2, "childrenAges[]": [6, 8], "durationFrom": 5, "durationTo": 9,
        "limit": 100, "language": "lav", "sort": "price_asc", "offset": 0, "mealGroupFrom": "AI", "hotelRating": 4,
        "countryId[]": ["15", "18"], "priceUntil": 1000, "operator[]": ["novaturas"],
        "tripAdvisorRatingFrom": 4}
    assert second[2]["json"]["offset"] == 2
    assert second[2]["json"]["limit"] == 100
    assert second[2]["json"]["lastListPrice"] == 600
    assert second[2]["json"]["excludeHotelIds"] == [1, 2]
    assert second[2]["json"]["departureAirport"] == ["VNO"]
    assert second[2]["json"]["childrenAges"] == [6, 8]
    assert third[2]["json"]["offset"] == 3
    assert third[2]["json"]["lastListPrice"] == 700
    assert third[2]["json"]["excludeHotelIds"] == [1, 2, 3]


def test_explicit_smaller_page_size_is_preserved_in_get_and_post():
    client, session = make_client([[row(1)], []], page_size=3)
    assert len(list(search(client))) == 1 and client.exhausted
    assert session.calls[0][2]["params"]["limit"] == 3
    assert session.calls[1][2]["json"]["limit"] == 3


def test_short_page_is_not_exhaustion_and_request_budget_preserves_partial_rows():
    client, session = make_client([[row(1)], [row(2)]], max_requests=2)
    seen = []
    with pytest.raises(discovery.WaavoDiscoveryBudgetError, match="request_budget"):
        for raw in search(client):
            seen.append(raw)
    assert len(seen) == 2 and len(session.calls) == 2
    assert not client.exhausted


def test_empty_stream_does_not_certify_inventory_or_reset_client_budget():
    client, session = make_client([[]], max_requests=1)
    assert list(search(client)) == [] and client.exhausted
    assert client.discovery_contract == "filtered_hotel_discovery_v1"
    assert client.inventory_complete is False and not hasattr(client, "inventory_contract")
    with pytest.raises(discovery.WaavoDiscoveryBudgetError):
        list(search(client))
    assert len(session.calls) == 1 and not client.exhausted


@pytest.mark.parametrize("second", [[row(1)], [row(1, price=600)]])
def test_repeated_or_nonadvancing_page_is_partial_before_duplicate_yield(second):
    client, session = make_client([[row(1)], second])
    seen = []
    with pytest.raises(discovery.WaavoDiscoveryError, match="repeated_page"):
        for raw in search(client):
            seen.append(raw)
    assert len(seen) == 1 and len(session.calls) == 2 and not client.exhausted


@pytest.mark.parametrize("status", [301, 400, 403, 429, 500])
def test_http_failures_never_retry_or_log_response_data(status):
    response = Response(status=status, body=b"private-vendor-diagnostic")
    client, session = make_client([response, []])
    with pytest.raises(waavo.WaavoError) as error:
        list(search(client))
    assert str(status) in str(error.value)
    assert "private-vendor-diagnostic" not in str(error.value)
    assert response.closed and len(session.calls) == 1 and not client.exhausted


@pytest.mark.parametrize("data", [None, [], {}, {"results": None}, {"results": {}},
                                   {"results": [None]}, {"error": "private", "results": []}])
def test_malformed_200_never_becomes_empty_success(data):
    response = Response(data)
    client, session = make_client([response])
    with pytest.raises(discovery.WaavoDiscoveryError, match="invalid_envelope"):
        list(search(client))
    assert response.closed and not client.exhausted and len(session.calls) == 1


def test_non_json_and_oversize_responses_are_bounded(monkeypatch):
    for body, code in [(b"private-non-json", "invalid_response"), (b"x" * 101, "response_too_large")]:
        monkeypatch.setattr(discovery, "MAX_RESPONSE_BYTES", 100)
        response = Response(body=body)
        client, _ = make_client([response])
        with pytest.raises(discovery.WaavoDiscoveryError, match=code):
            list(search(client))
        assert response.closed and not client.exhausted


def test_transport_failure_never_retries():
    client, session = make_client([requests.Timeout("private-url"), []])
    with pytest.raises(discovery.WaavoDiscoveryError, match="^waavo_discovery_transport_error$"):
        list(search(client))
    assert len(session.calls) == 1


def test_expired_deadline_sends_no_http(monkeypatch):
    monkeypatch.setattr(waavo.time, "monotonic", lambda: 100)
    client, session = make_client([[]], deadline=99)
    with pytest.raises(waavo.WaavoDeadlineError):
        list(search(client))
    assert not session.calls


def test_new_traversal_never_resumes_an_abandoned_cursor():
    client, session = make_client([[row(1)], []])
    iterator = search(client)
    assert next(iterator)["hotelId"] == 1
    iterator.close()
    assert list(search(client)) == []
    assert [call[0] for call in session.calls] == ["GET", "GET"]
    assert session.calls[1][2]["params"]["offset"] == 0


@pytest.mark.parametrize("changes", [{"meal_group": "AI,BB"}, {"meal_group": "UAI"},
    {"meal_group": "FB"}, {"country_ids": ["country:TR"]},
    {"country_ids": [True]}, {"stars_min": True}, {"budget_max": float("nan")},
    {"tripadvisor_rating_min": float("inf")}, {"children_ages": [False]}, {"duration_till": 30}])
def test_invalid_filters_fail_before_http(changes):
    client, session = make_client([[]])
    with pytest.raises(ValueError):
        list(search(client, **changes))
    assert not session.calls


def test_flat_normalization_preserves_actual_terms_and_never_promotes_deal_identity():
    raw = row(42)
    before = deepcopy(raw)
    hotel, offer, review = discovery.normalize(raw, 1, origin="VNO")
    assert raw == before
    assert hotel["source_hotel_id"] == "novaturas:42" and hotel["country_id"] == "15"
    assert offer["origin_id"] == "VNO" and offer["pax_adl"] == 1 and offer["pax_chd"] == 0
    assert offer["date_start"] == "2026-10-20" and offer["nights"] == 7
    assert offer["price_cents"] == 49900 and offer["currency"] == "EUR"
    assert offer["board_code"] == "AI" and offer["room_name"] == "Standard"
    assert offer["link"] == raw["link"] and offer["room_code"].startswith("wu2:")
    assert offer["room_placement"] == ""
    assert review["rating"] == 4.2 and review["reviews_count"] == 32
    assert review["match_status"] == "ok"
    changed = discovery.normalize(row(42, price=450, link="https://example.invalid/new"), 1, origin="VNO")[1]
    assert changed["room_code"] == offer["room_code"]
    assert discovery.normalize(row(42, mealGroupCode="BB"), 1, origin="VNO")[1]["room_code"] != offer["room_code"]


@pytest.mark.parametrize("field,value", [("adults", None), ("adults", 2), ("adults", True),
    ("children", None), ("children", 1), ("childrenAge", None), ("childrenAge", ""),
    ("departureAirportCode", None), ("departureAirportCode", "RIX"),
    ("currency", None), ("currency", "USD"), ("date", "bad"),
    ("duration", None), ("duration", True), ("price", 0), ("price", float("nan")),
    ("operatorCode", None), ("hotelId", None), ("countryName", None),
    ("countryName", ""), ("countryName", "  "), ("countryName", 18)])
def test_missing_or_conflicting_flat_evidence_never_fills_from_query(field, value):
    with pytest.raises(discovery.WaavoDiscoveryError):
        discovery.normalize(row(1, **{field: value}), 1, origin="VNO")


def test_flat_child_ages_require_explicit_exact_multiset():
    raw = row(1, children=2, childrenAge=[8, 6])
    assert discovery.normalize(raw, 1, [6, 8], origin="VNO")[1]["children_ages"] == "6,8"
    with pytest.raises(discovery.WaavoDiscoveryError, match="party_mismatch"):
        discovery.normalize(raw, 1, [6, 6], origin="VNO")


def test_missing_link_is_not_repaired_and_joinup_skip_uses_flat_operator():
    assert discovery.normalize(row(1, link=None), 1, origin="VNO")[1]["link"] is None
    assert discovery.should_skip(row(1, operatorCode="joinup"))
    assert not discovery.should_skip(row(1))


def test_cheap_country_namespace_uses_observed_name_and_rejects_contradictions():
    from tourfinder.countries import canonical_country_id

    assert discovery.CHEAP_COUNTRY_IDS["EG"] == "18"
    assert discovery.CHEAP_COUNTRY_IDS["TR"] == "15"
    hotel, _, _ = discovery.normalize(row(1, countryId=18, countryName="Egypt"), 1, origin="VNO")
    assert canonical_country_id(hotel["source"], hotel["country_id"], hotel["country_name"]) == "country:EG"
    with pytest.raises(discovery.WaavoDiscoveryError, match="country_mismatch"):
        discovery.normalize(row(1, countryId=18, countryName="Turkey"), 1, origin="VNO")
    hotel, _, _ = discovery.normalize(row(1, countryId=18, countryName="Unrecognized"), 1, origin="VNO")
    assert canonical_country_id(hotel["source"], hotel["country_id"], hotel["country_name"]) == "18"
    assert hotel["country_name"] == "Unrecognized"


@pytest.mark.parametrize("images", [None, "https://example.invalid/not-a-list", {}, [None, {}, ""]])
def test_malformed_optional_images_cannot_become_a_fabricated_character_url(images):
    hotel, _, _ = discovery.normalize(row(1, images=images), 1, origin="VNO")
    assert hotel["photo_url"] is None
