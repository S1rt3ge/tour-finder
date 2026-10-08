"""Waavo aggregator source (joinastra.waavo.com).

One JSON API returns offers from every Baltic operator at once, with
built-in TripAdvisor ratings and a was/now price. See docs/waavo-recon.md.

We take Waavo for every operator EXCEPT Join Up — Join Up we collect
directly (`sources/joinup.py`), where coverage is complete; the aggregator
demonstrably drops some Join Up offers.
"""
import hashlib
import json
import logging
import math
import random
import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation

import requests

log = logging.getLogger(__name__)

SOURCE_NAME = "waavo"
BASE_URL = "https://joinastra.waavo.com/api/v1/travels/search"
RIGA_AIRPORT = "RIX"
PAGE_SIZE = 100
MAX_PRICE_CENTS = 2_147_483_647  # PostgreSQL INTEGER, also used by snapshots.
# Join Up is collected directly (fuller), so skip it here to avoid a worse
# duplicate. Everything else is only reachable through the aggregator.
EXCLUDE_OPERATORS = {"joinup"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class WaavoError(RuntimeError):
    pass


class WaavoBlockedError(WaavoError):
    """Source refused us (403). Abort the run, do not retry."""


class WaavoClient:
    def __init__(self, delay: float = 1.2, session: requests.Session | None = None):
        self.delay = delay
        self.requests_made = 0
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": "https://joinastra.waavo.com/",
        })

    def _get(self, **params) -> dict:
        for attempt in range(4):
            time.sleep(self.delay + random.uniform(0, 0.6))
            try:
                resp = self.session.get(BASE_URL, params=params, timeout=60)
            except (requests.Timeout, requests.ConnectionError) as exc:
                self.requests_made += 1
                wait = 5 * (attempt + 1)
                log.warning("waavo -> %s, retry in %ss", type(exc).__name__, wait)
                time.sleep(wait)
                continue
            self.requests_made += 1
            if resp.status_code == 403:
                raise WaavoBlockedError("403 from waavo")
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = 5 * (attempt + 1)
                log.warning("waavo -> HTTP %s, retry in %ss", resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise WaavoError("retries exhausted")

    def search_pages(self, date_from: str, date_till: str, adults: int,
                     children_ages: list[int] | None = None,
                     duration_from: int = 2, duration_till: int = 21,
                     max_pages: int | None = None):
        """Yield raw offers across offset pages. Operator filtering isn't
        honored server-side, so callers drop excluded operators."""
        offset = 0
        page = 0
        while True:
            params = dict(departureAirport=RIGA_AIRPORT, dateFrom=date_from,
                          dateTo=date_till, adults=adults,
                          durationFrom=duration_from, durationTo=duration_till,
                          limit=PAGE_SIZE, offset=offset)
            if children_ages:
                params["children"] = len(children_ages)
                # plural! `childrenAge` (singular) 400s: "Children(1) does
                # not match children ages: " — verified live 2026-07-27.
                params["childrenAges"] = ",".join(str(a) for a in children_ages)
            data = self._get(**params)
            offers = ((data or {}).get("data") or {}).get("offers") or []
            yield from offers
            page += 1
            if len(offers) < PAGE_SIZE or (max_pages and page >= max_pages):
                return
            offset += PAGE_SIZE


def to_cents(value) -> int | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal(MAX_PRICE_CENTS) / 100:
            return None
        cents = int(amount * 100)
        return cents if cents > 0 else None
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None


def _f(value) -> float | None:
    try:
        if value in (None, "") or isinstance(value, bool):
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _integer(value) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or number != number.to_integral_value()
                or abs(number) > MAX_PRICE_CENTS):
            return None
        return int(number)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _validate_pax(offer: dict, adults: int, children_ages: list[int]) -> bool:
    """Reject explicit conflicting/malformed echoes; missing echoes are unverified."""
    if type(adults) is not int or not 1 <= adults <= 6:
        raise ValueError("waavo_invalid_party")
    if (len(children_ages) > 4 or any(type(age) is not int or not 0 <= age <= 17
                                     for age in children_ages)):
        raise ValueError("waavo_invalid_party")
    for field, expected in (("adults", adults), ("children", len(children_ages))):
        if field in offer and _integer(offer[field]) != expected:
            raise ValueError("waavo_pax_echo_mismatch")
    age_fields = [key for key in ("childrenAge", "childrenAges") if key in offer]
    for field in age_fields:
        echoed = offer[field]
        if isinstance(echoed, str):
            echoed = echoed.split(",") if echoed.strip() else []
        if not isinstance(echoed, (list, tuple)):
            raise ValueError("waavo_pax_echo_mismatch")
        values = [_integer(value) for value in echoed]
        if any(value is None for value in values) or sorted(values) != sorted(children_ages):
            raise ValueError("waavo_pax_echo_mismatch")
    return "adults" in offer and "children" in offer and bool(age_fields)


def _room_identity(offer: dict, adults: int, children_ages: list[int],
                   *, verified_pax: bool) -> str:
    """Versioned terms fingerprint, never interchangeable with legacy room_code=''.

    wv2 requires the observed opaque provider key AND complete visible terms.
    wu2 keeps incomplete source rows usable for browsing, not deal evidence.
    Price-bearing URLs, pricing and observation age must not affect identity.
    If Waavo rotates offerKey with price, a missed comparison is safer than a
    guessed match. Unknown room/meal fields are retained conservatively.
    """
    room = _mapping(offer.get("room"))
    meal = _mapping(room.get("meal"))
    group = _mapping(meal.get("group"))
    departure = _mapping(offer.get("departureAirport"))
    arrival = _mapping(offer.get("arrivalAirport"))
    operator = _mapping(offer.get("operator"))
    provider_key = offer.get("offerKey")
    valid_key = isinstance(provider_key, str) and re.fullmatch(r"[0-9a-fA-F]{32}", provider_key) is not None
    terms = {
        "version": 2, "provider_key": provider_key,
        "hotel_id": _mapping(offer.get("hotel")).get("id"), "operator": operator,
        "room": room, "departure": departure, "arrival": arrival,
        "date": offer.get("date"), "duration": offer.get("duration"),
        "trip_duration": offer.get("tripDuration"),
        "transfer_included": offer.get("transferIncluded"),
        "adults": adults, "children_ages": children_ages,
        "currency": _mapping(offer.get("pricing")).get("currency"),
        "advance": offer.get("advance"), "warnings": offer.get("warnings"),
    }
    valid_date = False
    try:
        valid_date = date.fromisoformat(terms["date"]).isoformat() == terms["date"]
    except (TypeError, ValueError):
        pass
    known_text = lambda value: isinstance(value, str) and bool(value.strip())
    known_hotel = ((type(terms["hotel_id"]) is int and terms["hotel_id"] >= 0)
                   or known_text(terms["hotel_id"]))
    reliable = bool(valid_key and known_text(room.get("name")) and verified_pax
                    and known_hotel and known_text(operator.get("code"))
                    and known_text(departure.get("code")) and known_text(arrival.get("code"))
                    and (known_text(group.get("code")) or known_text(meal.get("translation")))
                    and valid_date and (_integer(terms["duration"]) or 0) > 0
                    and (_integer(terms["trip_duration"]) or 0) > 0
                    and isinstance(terms["transfer_included"], bool)
                    and known_text(terms["currency"]))
    try:
        canonical = json.dumps(terms, sort_keys=True, ensure_ascii=True,
                               separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        # Malformed non-JSON/non-finite terms can never become trusted evidence.
        reliable = False
        canonical = json.dumps(terms, sort_keys=True, ensure_ascii=True,
                               separators=(",", ":"), default=str)
    return ("wv2:" if reliable else "wu2:") + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize(offer: dict, adults: int, children_ages: list[int] | None = None):
    """Waavo offer -> (hotel row, offer row + snapshot fields, review row).

    Returns (hotel, offer, review) where review may be None. The requested pax
    is accepted only when any explicit source echoes agree with it.
    """
    if children_ages is not None and not isinstance(children_ages, (list, tuple)):
        raise ValueError("waavo_invalid_party")
    children_ages = list(children_ages or [])
    verified_pax = _validate_pax(offer, adults, children_ages)
    children_ages.sort()
    h = _mapping(offer.get("hotel"))
    room = _mapping(offer.get("room"))
    meal = _mapping(room.get("meal"))
    board = _mapping(meal.get("group"))
    region = _mapping(offer.get("region"))
    country = _mapping(region.get("country"))
    dep = _mapping(offer.get("departureAirport"))
    operator = _mapping(offer.get("operator")).get("code")
    pricing = _mapping(offer.get("pricing"))
    ta = _mapping(h.get("tripadvisor"))
    images = h.get("images") or []
    ages = ",".join(str(a) for a in sorted(children_ages)) if children_ages else ""

    # Same physical hotel is sold by several operators at different prices;
    # namespacing the id by operator keeps those as distinct offers without
    # touching the offer-identity unique key. Cross-operator grouping of the
    # same hotel is a later enhancement (needs fuzzy hotel matching, SPEC §9).
    hotel_id = str(h["id"]) if h.get("id") is not None else ""
    if operator and hotel_id:
        hotel_id = f"{operator}:{hotel_id}"

    hotel = {
        "source": SOURCE_NAME,
        "source_hotel_id": hotel_id,
        "name": h.get("name", ""),
        "category": str(h["starsCount"]) if h.get("starsCount") else None,
        "country_id": str(country.get("id") or ""),
        "country_name": country.get("name"),
        "city_name": region.get("name"),
        "latitude": _f(h.get("latitude")),
        "longitude": _f(h.get("longitude")),
        "photo_url": images[0] if images else None,
    }

    price_cents = to_cents(pricing.get("price"))
    offer_row = {
        "source": SOURCE_NAME,
        "source_hotel_id": hotel["source_hotel_id"],
        "origin_id": dep.get("code") or RIGA_AIRPORT,
        "origin_name": dep.get("name"),
        "date_start": offer.get("date"),
        "date_end": None,
        "nights": offer.get("duration") or offer.get("tripDuration"),
        "board_code": board.get("code") or "",
        "board_name": meal.get("translation"),
        "room_code": _room_identity(offer, adults, children_ages, verified_pax=verified_pax),
        "room_name": room.get("name"),
        "room_placement": "",
        "pax_adl": adults,
        "pax_chd": len(children_ages) if children_ages else 0,
        "children_ages": ages,
        "operator": operator,
        "link": offer.get("hotelUrl") or offer.get("reservationUrl"),
        # snapshot fields
        "price_cents": price_cents,
        "currency": pricing.get("currency", "EUR"),
        "availability": None,
        "stop_sale": None,
        # aggregator's own "before" price -> our operator-average slot,
        # so the "was/now" badge lights up from day one
        "operator_avg_price_cents": to_cents(pricing.get("priceBefore")),
    }

    review = None
    rating = _f(ta.get("rating"))
    if rating is not None and 0 <= rating <= 5:
        review_count = _integer(ta.get("ratingsCount"))
        review = {
            "source": SOURCE_NAME,
            "source_hotel_id": hotel["source_hotel_id"],
            "platform": "tripadvisor",
            "rating": rating,
            "rating_scale": 5,
            "reviews_count": review_count if review_count is not None and review_count >= 0 else None,
            "summary": None,
            "external_id": None,
            "url": None,
            "matched_name": h.get("name"),
            "match_status": "ok",
        }
    return hotel, offer_row, review


def should_skip(offer: dict) -> bool:
    return (offer.get("operator") or {}).get("code") in EXCLUDE_OPERATORS
