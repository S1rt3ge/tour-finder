"""Bounded filtered hotel discovery; never proof of exhaustive tour inventory.

The official Waavo frontend uses GET for its first cheap-search page and POST
with an in-memory price/hotel cursor for continuation. See docs/waavo-recon.md.
``exhausted`` means this particular response stream returned an empty page. It
does not certify all operators, rooms, meals or dates, including with operator=None.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re

import requests
from urllib3.util import Timeout

from . import waavo
from ..countries import canonical_country_id
from ..origins import normalize_source_origin, source_origin

BASE_URL = "https://joinastra.waavo.com/api/v1/cheap_travels_search/"
DISCOVERY_CONTRACT = "filtered_hotel_discovery_v1"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# Official cheap_travels_filters snapshot, 2026-10-09. This namespace differs
# from the legacy nested inventory API: 18 is Egypt here, not Turkey.
CHEAP_COUNTRY_IDS = {
    "AD": "54", "AE": "37", "AL": "65", "AT": "36", "BG": "17", "CO": "14",
    "CY": "56", "EG": "18", "ES": "1", "FR": "3", "GR": "28", "HR": "41",
    "ID": "24", "IT": "10", "KE": "35", "LK": "94", "LT": "89", "MA": "38",
    "ME": "88", "MU": "50", "PT": "5", "TH": "4", "TN": "52", "TR": "15",
    "TZ": "131", "VN": "71",
}
CHEAP_MEAL_THRESHOLDS = frozenset(("RO", "BB", "HB", "AI"))


class WaavoDiscoveryError(waavo.WaavoError):
    pass


class WaavoDiscoveryBudgetError(WaavoDiscoveryError):
    pass


def _number(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("waavo_discovery_invalid_filter")
    return value


def _party(adults, children_ages):
    adults = _number(adults, 1, 6)
    ages = [] if children_ages is None else children_ages
    if not isinstance(ages, (list, tuple)) or len(ages) > 4:
        raise ValueError("waavo_discovery_invalid_filter")
    return adults, sorted(_number(age, 0, 17) for age in ages)


def _date(value):
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except (ValueError, TypeError):
        pass
    raise ValueError("waavo_discovery_invalid_date")


def _code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
        raise ValueError("waavo_discovery_invalid_filter")
    return value


def _hotel_id(value):
    if type(value) is int and value >= 0:
        return str(value)
    if isinstance(value, str) and value.strip() and len(value) <= 128:
        return value
    raise WaavoDiscoveryError("waavo_discovery_invalid_hotel_id")


def _price(value):
    if isinstance(value, bool):
        raise ValueError("waavo_discovery_invalid_price")
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and Decimal("0.01") <= amount <= Decimal(waavo.MAX_PRICE_CENTS) / 100:
            return float(amount)
    except (InvalidOperation, ValueError, TypeError):
        pass
    raise ValueError("waavo_discovery_invalid_price")


class WaavoDiscoveryClient(waavo.WaavoClient):
    discovery_contract = DISCOVERY_CONTRACT
    inventory_complete = False

    def __init__(self, delay=1.2, session=None, deadline=None, max_requests=6, page_size=100):
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
            raise ValueError("waavo_discovery_invalid_delay")
        super().__init__(delay=delay, session=session, deadline=deadline)
        self.max_requests = _number(max_requests, 1, 100)
        # Official frontend requests 20 list rows or 400 map rows. Use a bounded
        # 100-row page to cover more hotels per shared request/deadline budget.
        self.page_size = _number(page_size, 1, 100)
        self.exhausted = False

    def _request(self, method, payload):
        self._remaining()
        if self.requests_made >= self.max_requests:
            raise WaavoDiscoveryBudgetError("partial: waavo_discovery_request_budget")
        self._pause(self.delay)
        remaining = self._remaining()
        timeout = 60 if remaining is None else Timeout(
            total=remaining, connect=min(60, remaining), read=min(60, remaining))
        self.requests_made += 1
        try:
            call = self.session.get if method == "GET" else self.session.post
            response = call(BASE_URL, **({"params": payload} if method == "GET" else {"json": payload}),
                            timeout=timeout, stream=True, allow_redirects=False)
        except (requests.Timeout, requests.ConnectionError):
            raise WaavoDiscoveryError("waavo_discovery_transport_error") from None
        try:
            if response.status_code in (403, 429):
                raise waavo.WaavoBlockedError(f"waavo_discovery_http_{response.status_code}")
            if response.status_code != 200:
                raise WaavoDiscoveryError(f"waavo_discovery_http_{response.status_code}")
            chunks, size = [], 0
            try:
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise WaavoDiscoveryError("waavo_discovery_response_too_large")
                    chunks.append(chunk)
                data = json.loads(b"".join(chunks))
            except (requests.RequestException, ValueError):
                raise WaavoDiscoveryError("waavo_discovery_invalid_response") from None
            if (not isinstance(data, dict) or data.get("error") or data.get("errors")
                    or not isinstance(data.get("results"), list)
                    or any(not isinstance(row, dict) for row in data["results"])):
                raise WaavoDiscoveryError("waavo_discovery_invalid_envelope")
            if len(data["results"]) > 1000:
                raise WaavoDiscoveryError("waavo_discovery_response_too_large")
            return data["results"]
        finally:
            response.close()

    def search_pages(self, date_from, date_till, adults, children_ages=None,
                     duration_from=2, duration_till=21, origin="RIX", *,
                     meal_group=None, stars_min=None, country_ids=None,
                     budget_max=None, operator=None, tripadvisor_rating_min=None):
        """Yield real flat rows; the cursor is local to this one traversal.

        budget_max is EUR for the entire party. Food/stars/Tripadvisor are
        minimum thresholds; exact food selections still require local filters.
        No inventory_contract is set.
        Request counts are cumulative for this client, not reset per traversal.
        """
        self.exhausted = False
        first, last = _date(date_from), _date(date_till)
        if first > last:
            raise ValueError("waavo_discovery_invalid_date")
        adults, ages = _party(adults, children_ages)
        low, high = _number(duration_from, 2, 21), _number(duration_till, 2, 21)
        if low > high:
            raise ValueError("waavo_discovery_invalid_filter")
        body = {"departureAirport": [source_origin("waavo", origin)], "dateFrom": first,
                "dateTo": last, "adults": adults, "children": len(ages),
                "durationFrom": low, "durationTo": high, "limit": self.page_size,
                "language": "lav", "sort": "price_asc"}
        if ages:
            body["childrenAges"] = ages
        if meal_group is not None:
            if _code(meal_group) not in CHEAP_MEAL_THRESHOLDS:
                raise ValueError("waavo_discovery_invalid_filter")
            body["mealGroupFrom"] = meal_group
        if stars_min is not None:
            body["hotelRating"] = _number(stars_min, 1, 5)
        if country_ids is not None:
            if not isinstance(country_ids, (list, tuple)) or not country_ids or len(country_ids) > 50:
                raise ValueError("waavo_discovery_invalid_filter")
            ids = []
            for value in country_ids:
                if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,12}", str(value)):
                    raise ValueError("waavo_discovery_invalid_filter")
                ids.append(str(value))
            body["countryId"] = sorted(set(ids))
        if budget_max is not None:
            body["priceUntil"] = _price(budget_max)
        if operator is not None:
            body["operator"] = [_code(operator)]
        if tripadvisor_rating_min is not None:
            if (isinstance(tripadvisor_rating_min, bool) or not isinstance(tripadvisor_rating_min, (int, float))
                    or not math.isfinite(tripadvisor_rating_min) or not 0 <= tripadvisor_rating_min <= 5):
                raise ValueError("waavo_discovery_invalid_filter")
            body["tripAdvisorRatingFrom"] = tripadvisor_rating_min
        offset, last_price = 0, None
        seen_hotels, seen_pages = {}, set()
        while True:
            payload = {**body, "offset": offset}
            if offset:
                payload.update(lastListPrice=last_price, excludeHotelIds=list(seen_hotels.values()))
                method = "POST"
            else:
                payload = {key + "[]" if isinstance(value, list) else key: value for key, value in payload.items()}
                method = "GET"
            rows = self._request(method, payload)
            if not rows:
                self.exhausted = True
                return
            try:
                signature = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=True,
                                                      allow_nan=False, separators=(",", ":")).encode()).digest()
                ids = {_hotel_id(row.get("hotelId")): row["hotelId"] for row in rows}
                last_price = _price(rows[-1].get("price"))
            except (ValueError, TypeError):
                raise WaavoDiscoveryError("waavo_discovery_invalid_cursor") from None
            if signature in seen_pages or not ids.keys() - seen_hotels.keys():
                raise WaavoDiscoveryError("partial: waavo_discovery_repeated_page")
            seen_pages.add(signature)
            # Parent validates/stores each row before asking us for the next.
            yield from rows
            offset += len(rows)
            seen_hotels.update(ids)


def normalize(raw, adults, children_ages=None, *, origin="RIX"):
    """Flat source facts only; missing echoes cannot be filled from the query."""
    adults, ages = _party(adults, children_ages)
    requested = source_origin("waavo", origin)
    if not isinstance(raw, dict):
        raise WaavoDiscoveryError("waavo_discovery_invalid_offer")
    echoed_ages = raw.get("childrenAge")
    if (waavo._integer(raw.get("adults")) != adults or waavo._integer(raw.get("children")) != len(ages)
            or not isinstance(echoed_ages, list)
            or any(type(value) is not int or not 0 <= value <= 17 for value in echoed_ages)
            or sorted(echoed_ages) != ages):
        raise WaavoDiscoveryError("waavo_discovery_party_mismatch")
    if normalize_source_origin("waavo", raw.get("departureAirportCode")) != requested:
        raise WaavoDiscoveryError("waavo_discovery_origin_mismatch")
    if raw.get("currency") != "EUR":
        raise WaavoDiscoveryError("waavo_discovery_currency_mismatch")
    country_name = raw.get("countryName")
    if not isinstance(country_name, str) or not country_name.strip():
        # An absent name would trigger the incompatible legacy country-ID map.
        raise WaavoDiscoveryError("waavo_discovery_country_missing")
    country_id = raw.get("countryId")
    named_country = canonical_country_id("", None, country_name)
    if (named_country and named_country.startswith("country:")
            and country_id is not None
            and CHEAP_COUNTRY_IDS.get(named_country[8:]) != str(country_id)):
        raise WaavoDiscoveryError("waavo_discovery_country_mismatch")
    try:
        _date(raw.get("date"))
        _price(raw.get("price"))
        nights = waavo._integer(raw.get("duration"))
        if nights is None or nights <= 0:
            raise ValueError("invalid nights")
        operator = _code(raw.get("operatorCode"))
    except ValueError:
        raise WaavoDiscoveryError("waavo_discovery_invalid_terms") from None
    hotel_id = _hotel_id(raw.get("hotelId"))
    images = raw.get("images")
    images = [value for value in images if isinstance(value, str) and value] if isinstance(images, list) else []
    nested = {
        "offerKey": raw.get("offerKey"),
        "hotel": {"id": hotel_id, "name": raw.get("hotelName", ""), "starsCount": raw.get("hotelRating"),
                  "latitude": raw.get("hotelLatitude"), "longitude": raw.get("hotelLongitude"),
                  "images": images, "tripadvisor": {"rating": raw.get("tripadvisorRating"),
                                                               "ratingsCount": raw.get("tripadvisorRatingsCount")}},
        "operator": {"code": operator},
        "departureAirport": {"code": raw["departureAirportCode"], "name": raw.get("departureAirport")},
        "region": {"name": raw.get("cityName"), "country": {"id": country_id, "name": country_name}},
        "room": {"name": raw.get("roomName"), "meal": {"id": raw.get("mealId"), "translation": raw.get("mealTranslation"),
                                                        "group": {"id": raw.get("mealGroupId"), "code": raw.get("mealGroupCode")}}},
        "date": raw["date"], "duration": nights, "tripDuration": raw.get("tripDuration"),
        "adults": adults, "children": len(ages), "childrenAge": echoed_ages,
        "pricing": {"price": raw["price"], "currency": raw["currency"], "priceBefore": raw.get("priceBefore")},
        "transferIncluded": raw.get("transferIncluded"), "hotelUrl": raw.get("link"),
    }
    hotel, offer, review = waavo.normalize(nested, adults, ages, origin=requested)
    # The flat discovery response does not supply full room/arrival terms.
    offer["room_code"] = "wu2:" + offer["room_code"].split(":", 1)[1]
    return hotel, offer, review


def should_skip(raw):
    return isinstance(raw, dict) and raw.get("operatorCode") in waavo.EXCLUDE_OPERATORS
