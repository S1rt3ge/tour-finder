"""Waavo identity/number safety: offline source-shaped fixtures, no live API."""
from copy import deepcopy
from decimal import Decimal
import re

import pytest
from sqlalchemy import create_engine

from tourfinder import db, fetcher
from tourfinder.sources import waavo


@pytest.fixture
def raw_offer():
    # Shape observed in the API: prices and URLs vary independently of terms.
    return {
        "offerKey": "475ea5e8a89d4a88b90b2804d57e84d9",
        "hotel": {"id": 7290864, "name": "Fixture hotel", "starsCount": 3,
                  "tripadvisor": {"rating": 4.4, "ratingsCount": 96}},
        "operator": {"code": "novaturas"},
        "room": {"name": "Double Room", "meal": {
            "id": 1, "translation": "Room only", "group": {"id": 1, "code": "RO"}}},
        "departureAirport": {"code": "RIX", "name": "Riga Intl"},
        "arrivalAirport": {"code": "TIV", "name": "Tivat"},
        "adults": 2, "children": 0, "childrenAge": [],
        "date": "2026-10-11", "duration": 7, "tripDuration": 7,
        "transferIncluded": True, "advance": None, "warnings": [],
        "pricing": {"price": 578, "priceBefore": 600, "currency": "EUR"},
        "priceAgeSeconds": 468,
        "hotelUrl": "https://example.invalid/hotel?price=578",
        "reservationUrl": "https://example.invalid/reserve?price=578",
    }


def row(raw, adults=2, ages=None):
    return waavo.normalize(raw, adults, ages)[1]


def replace_path(raw, path, value):
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def test_real_shape_double_and_triple_room_do_not_collapse(raw_offer):
    triple = deepcopy(raw_offer)
    triple["room"]["name"] = "Triple Room"
    triple["offerKey"] = "8a6dddcba335a621a4fd86b6f0673e20"
    triple["pricing"]["price"] = 658
    double_row, triple_row = row(raw_offer), row(triple)
    assert double_row["source_hotel_id"] == triple_row["source_hotel_id"]
    assert double_row["date_start"] == triple_row["date_start"]
    assert double_row["board_code"] == triple_row["board_code"]
    assert (double_row["price_cents"], triple_row["price_cents"]) == (57800, 65800)
    assert re.fullmatch(r"wv2:[0-9a-f]{64}", double_row["room_code"])
    assert re.fullmatch(r"wv2:[0-9a-f]{64}", triple_row["room_code"])
    assert double_row["room_code"] != triple_row["room_code"]


@pytest.mark.parametrize("path,value", [
    (("room", "name"), "Triple Room"),
    (("room", "placement"), "2 adults + extra bed"),
    (("room", "meal", "id"), 77),
    (("room", "meal", "translation"), "Soft all inclusive"),
    (("room", "meal", "group", "code"), "AI"),
    (("room", "meal", "group", "id"), 22),
    (("offerKey",), "b" * 32),
    (("arrivalAirport", "code"), "TGD"),
    (("departureAirport", "code"), "VNO"),
    (("transferIncluded",), False),
    (("tripDuration",), 8),
    (("duration",), 6),
    (("date",), "2026-10-12"),
    (("operator", "code"), "teztour"),
    (("hotel", "id"), 123),
    (("pricing", "currency"), "USD"),
    (("advance",), {"nonRefundable": True}),
    (("warnings",), ["Transfer excluded"]),
])
def test_changed_terms_split_even_if_provider_key_is_reused(raw_offer, path, value):
    altered = deepcopy(raw_offer)
    replace_path(altered, path, value)
    assert row(raw_offer)["room_code"] != row(altered)["room_code"]


def test_prices_links_age_reviews_and_dict_order_do_not_change_identity(raw_offer):
    baseline = row(raw_offer)["room_code"]
    changed = deepcopy(raw_offer)
    changed["pricing"].update(price=498, priceBefore=900, pricePerPerson=249)
    changed["priceAgeSeconds"] = 0
    changed["hotelUrl"] = "https://example.invalid/hotel?price=498"
    changed["reservationUrl"] = "https://example.invalid/other?price=498"
    changed["hotel"]["tripadvisor"]["rating"] = 4.5
    changed["hotel"]["images"] = ["https://example.invalid/new.jpg"]
    changed["room"]["meal"] = dict(reversed(list(changed["room"]["meal"].items())))
    assert row(changed)["price_cents"] == 49800
    assert row(changed)["room_code"] == baseline


@pytest.mark.parametrize("key", [None, "", "arbitrary-id", "a" * 31, "g" * 32, "a" * 33, 123])
def test_bad_provider_key_is_uncertain_never_legacy(raw_offer, key):
    raw_offer["offerKey"] = key
    identity = row(raw_offer)["room_code"]
    assert re.fullmatch(r"wu2:[0-9a-f]{64}", identity)
    assert identity != ""


@pytest.mark.parametrize("path", [
    ("offerKey",), ("room", "name"), ("arrivalAirport", "code"),
    ("departureAirport", "code"), ("transferIncluded",), ("tripDuration",),
    ("adults",), ("children",), ("childrenAge",),
])
def test_missing_required_evidence_remains_uncertain(raw_offer, path):
    target = raw_offer
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    assert row(raw_offer)["room_code"].startswith("wu2:")


def test_uncertain_fixture_identity_survives_price_and_link_changes(raw_offer):
    for key in ("offerKey", "adults", "children", "childrenAge"):
        raw_offer.pop(key)
    before = row(raw_offer)["room_code"]
    raw_offer["pricing"]["price"] = 400
    raw_offer["hotelUrl"] = "https://example.invalid/changed"
    assert row(raw_offer)["room_code"] == before
    assert before.startswith("wu2:")
    raw_offer["room"]["name"] = "Triple Room"
    assert row(raw_offer)["room_code"] != before


@pytest.mark.parametrize("echo", [
    {"adults": 3}, {"adults": "NaN"}, {"adults": True}, {"adults": None},
    {"children": 1}, {"children": "Infinity"}, {"childrenAge": [6]},
    {"childrenAge": [None]}, {"childrenAge": ["NaN"]}, {"childrenAge": None},
    {"childrenAges": [6]},
])
def test_explicit_pax_mismatch_is_not_stored_as_requested_party(raw_offer, echo):
    raw_offer.update(echo)
    with pytest.raises(ValueError, match="^waavo_pax_echo_mismatch$"):
        row(raw_offer)


def test_children_age_order_is_canonical_and_both_echo_spellings_checked(raw_offer):
    raw_offer.update(adults="2", children="2", childrenAge=[8, "6"], childrenAges="6,8")
    first = row(raw_offer, ages=[8, 6])
    second = row(raw_offer, ages=[6, 8])
    assert first["room_code"] == second["room_code"]
    assert first["room_code"].startswith("wv2:")
    assert first["children_ages"] == "6,8"
    assert first["pax_chd"] == 2
    raw_offer["childrenAge"] = [6, 9]
    with pytest.raises(ValueError, match="waavo_pax_echo_mismatch"):
        row(raw_offer, ages=[6, 8])


@pytest.mark.parametrize("value", [None, "", "bad", True, False, "NaN", "sNaN",
                                      "Infinity", "-Infinity", float("nan"), float("inf"),
                                      Decimal("NaN"), 0, -1, "0.001", "1e1000000", "21474836.48"])
def test_invalid_prices_become_missing_without_numeric_exceptions(value):
    assert waavo.to_cents(value) is None


@pytest.mark.parametrize("value,expected", [(578, 57800), (19.99, 1999), ("0.29", 29),
                                            ("1.239", 123), ("21474836.47", 2147483647)])
def test_valid_prices_use_decimal_cents(value, expected):
    assert waavo.to_cents(value) == expected


@pytest.mark.parametrize("value", ["NaN", "Infinity", float("-inf"), "bad", 5.1, -1, True])
def test_invalid_rating_never_becomes_confirmed_review(raw_offer, value):
    raw_offer["hotel"]["tripadvisor"]["rating"] = value
    assert waavo.normalize(raw_offer, 2)[2] is None


def test_nonfinite_coordinates_counts_and_identity_terms_fail_safely(raw_offer):
    raw_offer["hotel"].update(latitude="NaN", longitude="Infinity")
    raw_offer["hotel"]["tripadvisor"]["ratingsCount"] = "NaN"
    raw_offer["room"]["size"] = float("nan")
    hotel, offer, review = waavo.normalize(raw_offer, 2)
    assert hotel["latitude"] is None and hotel["longitude"] is None
    assert review["rating"] == 4.4 and review["reviews_count"] is None
    assert offer["room_code"].startswith("wu2:")


def test_normalizing_invalid_current_or_marketing_price_never_changes_to_another_price(raw_offer):
    raw_offer["pricing"].update(price="Infinity", priceBefore="NaN")
    offer = row(raw_offer)
    assert offer["price_cents"] is None
    assert offer["operator_avg_price_cents"] is None


def test_normalize_does_not_mutate_source_or_requested_ages(raw_offer):
    raw_offer.update(children=2, childrenAge=[6, 8])
    original = deepcopy(raw_offer)
    ages = [8, 6]
    row(raw_offer, ages=ages)
    assert raw_offer == original
    assert ages == [8, 6]


def test_new_identity_never_rewrites_or_reuses_legacy_history(raw_offer, tmp_path):
    # Explicit isolated SQLite: never touch DATABASE_URL or a production engine.
    engine = create_engine(f"sqlite:///{(tmp_path / 'waavo.sqlite').as_posix()}")
    db.metadata.create_all(engine)
    connection = db.DB(engine)
    try:
        hotel, offer, review = waavo.normalize(raw_offer, 2)
        legacy_run = fetcher._start_run(connection, "waavo", "near", "2", {})
        writer = fetcher._BatchWriter(connection, legacy_run)
        legacy = {**offer, "room_code": "", "price_cents": 99900}
        writer.add(hotel, legacy, review=review)
        writer.flush()
        legacy_id = connection.execute("SELECT id FROM offers WHERE room_code='' ").scalar()
        repaired_run = fetcher._start_run(connection, "waavo", "near", "2", {})
        repaired = fetcher._BatchWriter(connection, repaired_run)
        repaired.add(hotel, offer, review=review)
        repaired.flush()
        assert connection.execute("SELECT count(*) FROM offers").scalar() == 2
        assert connection.execute("SELECT price_cents FROM price_snapshots WHERE offer_id=:id",
                                  {"id": legacy_id}).scalar() == 99900
        new_id = connection.execute("SELECT id FROM offers WHERE room_code=:code",
                                   {"code": offer["room_code"]}).scalar()
        assert new_id != legacy_id
        assert connection.execute("SELECT price_cents FROM price_snapshots WHERE offer_id=:id",
                                  {"id": new_id}).scalar() == 57800
    finally:
        connection.close()
        engine.dispose()
