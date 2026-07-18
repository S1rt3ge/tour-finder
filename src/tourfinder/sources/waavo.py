"""Waavo aggregator source (joinastra.waavo.com).

One JSON API returns offers from every Baltic operator at once, with
built-in TripAdvisor ratings and a was/now price. See docs/waavo-recon.md.

We take Waavo for every operator EXCEPT Join Up — Join Up we collect
directly (`sources/joinup.py`), where coverage is complete; the aggregator
demonstrably drops some Join Up offers.
"""
import logging
import random
import time
from decimal import Decimal, InvalidOperation

import requests

log = logging.getLogger(__name__)

SOURCE_NAME = "waavo"
BASE_URL = "https://joinastra.waavo.com/api/v1/travels/search"
RIGA_AIRPORT = "RIX"
PAGE_SIZE = 100
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
                params["childrenAge"] = ",".join(str(a) for a in children_ages)
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
        return int(Decimal(str(value)) * 100)
    except (InvalidOperation, ValueError):
        return None


def _f(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalize(offer: dict, adults: int, children_ages: list[int] | None = None):
    """Waavo offer -> (hotel row, offer row + snapshot fields, review row).

    Returns (hotel, offer, review) where review may be None. pax comes from
    the query for consistency with the joinup source; Waavo echoes it too.
    """
    h = offer.get("hotel") or {}
    room = offer.get("room") or {}
    meal = room.get("meal") or {}
    board = (meal.get("group") or {})
    region = offer.get("region") or {}
    country = region.get("country") or {}
    dep = offer.get("departureAirport") or {}
    operator = (offer.get("operator") or {}).get("code")
    pricing = offer.get("pricing") or {}
    ta = h.get("tripadvisor") or {}
    images = h.get("images") or []
    ages = ",".join(str(a) for a in sorted(children_ages)) if children_ages else ""

    # Same physical hotel is sold by several operators at different prices;
    # namespacing the id by operator keeps those as distinct offers without
    # touching the offer-identity unique key. Cross-operator grouping of the
    # same hotel is a later enhancement (needs fuzzy hotel matching, SPEC §9).
    hotel_id = f"{operator}:{h.get('id')}" if operator else str(h.get("id") or "")

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
        "room_code": "",
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
    if ta.get("rating") is not None:
        review = {
            "source": SOURCE_NAME,
            "source_hotel_id": hotel["source_hotel_id"],
            "platform": "tripadvisor",
            "rating": _f(ta.get("rating")),
            "rating_scale": 5,
            "reviews_count": ta.get("ratingsCount"),
            "summary": None,
            "external_id": None,
            "url": None,
            "matched_name": h.get("name"),
            "match_status": "ok",
        }
    return hotel, offer_row, review


def should_skip(offer: dict) -> bool:
    return (offer.get("operator") or {}).get("code") in EXCLUDE_OPERATORS
