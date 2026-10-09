"""Airport-specific source contracts, using local dictionaries and fake HTTP."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from tourfinder.origins import JOINUP_ORIGINS
from tourfinder.sources import joinup, waavo


@pytest.mark.parametrize("origin", ["RIX", "VNO", "TLL"])
def test_source_request_parameters_use_exact_airport(origin):
    direct = joinup.JoinUpClient()
    direct._get = Mock(side_effect=[{"destinations": []}, {"stays": []}, {"tours": []}])
    assert direct.destinations(origin) == []
    assert direct.stays(origin, "c_9", "2026-10-10:2026-10-20") == []
    assert list(direct.search_pages(origin, "c_9", "2026-10-10:2026-10-20", "7", 2)) == []
    assert all(call.kwargs["origins"] == JOINUP_ORIGINS[origin] for call in direct._get.call_args_list)
    aggregate = waavo.WaavoClient()
    aggregate._get = Mock(return_value={"data": {"offers": []}})
    assert list(aggregate.search_pages("2026-10-10", "2026-10-20", 2, origin=origin)) == []
    assert aggregate._get.call_args.kwargs["departureAirport"] == origin


def direct_offer(echo=None):
    return {"hotel": {"id": "fixture", "name": "Fixture"}, "offers": [{
        "from": {} if echo is None else {"id": echo}, "date_start": "2026-10-20",
        "stay": {"stay": 7}, "board": {"board_type": "AI"},
        "price": {"total_price": {"price": 100}}, "rooms": [{"code": "standard", "placement": "2AD"}]}]}


def aggregator_offer(echo=None):
    return {"hotel": {"id": 1, "name": "Fixture"}, "operator": {"code": "teztour"},
        "date": "2026-10-20", "duration": 7, "pricing": {"price": 100, "currency": "EUR"},
        "departureAirport": {} if echo is None else {"code": echo}}


@pytest.mark.parametrize("origin", ["RIX", "VNO", "TLL"])
@pytest.mark.parametrize("echoed", [False, True])
def test_missing_echo_uses_requested_airport_and_known_echo_must_agree(origin, echoed):
    raw = direct_offer(JOINUP_ORIGINS[origin] if echoed else None)
    before = deepcopy(raw)
    normalized = joinup.normalize(raw, 2, origin=origin)[1][0]
    assert normalized["origin_id"] == JOINUP_ORIGINS[origin]
    assert f"origin={JOINUP_ORIGINS[origin]}" in normalized["link"]
    assert raw == before
    raw = aggregator_offer(origin if echoed else None)
    before = deepcopy(raw)
    assert waavo.normalize(raw, 2, origin=origin)[1]["origin_id"] == origin
    assert raw == before


@pytest.mark.parametrize("requested,echo", [("VNO", "RIX"), ("RIX", "TLL"), ("TLL", "JFK")])
def test_explicit_different_or_unknown_echo_is_never_relabelled(requested, echo):
    with pytest.raises(ValueError, match="origin_echo_mismatch"):
        waavo.normalize(aggregator_offer(echo), 2, origin=requested)
    with pytest.raises(ValueError, match="origin_echo_mismatch"):
        joinup.normalize(direct_offer(JOINUP_ORIGINS.get(echo, "999999")), 2, origin=requested)


@pytest.mark.parametrize("origin", ["", "JFK", "RIX,VNO", None])
def test_invalid_requested_airport_never_reaches_source(origin):
    direct = joinup.JoinUpClient()
    direct._get = Mock(side_effect=AssertionError("must not request"))
    with pytest.raises(ValueError):
        list(direct.search_pages(origin, "c_9", "2026-10-10", "7", 2))
    aggregate = waavo.WaavoClient()
    aggregate._get = Mock(side_effect=AssertionError("must not request"))
    with pytest.raises(ValueError):
        list(aggregate.search_pages("2026-10-10", "2026-10-20", 2, origin=origin))
    direct._get.assert_not_called()
    aggregate._get.assert_not_called()


def test_malformed_explicit_waavo_origin_is_not_treated_as_missing():
    raw = aggregator_offer()
    raw["departureAirport"] = "VNO"
    with pytest.raises(ValueError, match="origin_echo_mismatch"):
        waavo.normalize(raw, 2, origin="RIX")
