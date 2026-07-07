"""Join Up Baltic (joinup.lv) source client.

Undocumented JSON API mapped in docs/joinup-api-recon.md. Read-only GETs,
no auth. The API is not ours: throttle every request and stop the whole
run on 403 — continuing after a block only makes the block worse.
"""
import logging
import random
import time
from decimal import Decimal, InvalidOperation

import requests

log = logging.getLogger(__name__)

SOURCE_NAME = "joinup"
BASE_URL = "https://joinup.lv/api/main"
PUBLIC_HOTEL_URL = "https://joinup.lv/{lang}/hotel/{hotel_id}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

RIGA_ORIGIN_ID = "3164"
HOT_TOUR_TYPE = "hot_tour"


class JoinUpError(RuntimeError):
    pass


class JoinUpBlockedError(JoinUpError):
    """Source refused us (403). Abort the run, do not retry."""


class JoinUpClient:
    def __init__(self, lang: str = "lv", currency: str = "EUR",
                 delay: float = 1.2, session: requests.Session | None = None):
        self.lang = lang
        self.currency = currency
        self.delay = delay
        self.requests_made = 0
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.session.headers["Accept"] = "application/json"

    def _get(self, path: str, **params) -> dict:
        params.setdefault("lang", self.lang)
        params.setdefault("currency", self.currency)
        url = f"{BASE_URL}/{path}"
        for attempt in range(5):
            time.sleep(self.delay + random.uniform(0, 0.6))
            try:
                resp = self.session.get(url, params=params, timeout=60)
            except (requests.Timeout, requests.ConnectionError) as exc:
                self.requests_made += 1
                # Heavy queries can take 15s+ server-side; when the API starts
                # dropping connections it needs a real pause, not seconds.
                wait = 20 * (attempt + 1)
                log.warning("%s -> %s, retry in %ss", path, type(exc).__name__, wait)
                time.sleep(wait)
                continue
            self.requests_made += 1
            if resp.status_code == 403:
                raise JoinUpBlockedError(f"{path}: 403 from source")
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = 5 * (attempt + 1)
                log.warning("%s -> HTTP %s, retry in %ss", path, resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("errors"):
                raise JoinUpError(f"{path}: {data['errors']}")
            return data
        raise JoinUpError(f"{path}: retries exhausted")

    def destinations(self, origin: str) -> list[dict]:
        return self._get("tour/destinations", origins=origin)["destinations"]

    def stays(self, origin: str, destination: str, dates: str) -> list[int]:
        data = self._get("tour/stays", origins=origin, destinations=destination, dates=dates)
        return [s["stay"] for s in data.get("stays", [])]

    def search_pages(self, origin: str, destinations: str, dates: str, stays: str,
                     pax_adl: int, children_ages: list[int] | None = None,
                     tour_types: str | None = None, max_pages: int | None = None):
        """Yield raw tour dicts across all result pages of one search."""
        page = 1
        while True:
            params = dict(origins=origin, destinations=destinations, dates=dates,
                          stays=stays, pax_adl=pax_adl, page=page)
            if children_ages:
                params["pax_chd"] = len(children_ages)
                params["children_ages"] = ",".join(str(a) for a in children_ages)
            if tour_types:
                params["tour_types"] = tour_types
            data = self._get("tour/tours", **params)
            tours = data.get("tours", [])
            yield from tours
            last_page = (data.get("pagination") or {}).get("last_page") or 0
            if not tours or page >= last_page or (max_pages and page >= max_pages):
                return
            page += 1


def to_cents(value) -> int | None:
    if value is None:
        return None
    try:
        return int(Decimal(str(value)) * 100)
    except (InvalidOperation, ValueError):
        return None


def ages_str(children_ages: list[int] | None) -> str:
    """Canonical children-ages string for storage and matching: sorted, comma
    joined, e.g. [8,6] -> '6,8'. Empty for no children."""
    if not children_ages:
        return ""
    return ",".join(str(a) for a in sorted(children_ages))


def normalize(tour: dict, pax_adl: int, children_ages: list[int] | None = None,
              lang: str = "lv") -> tuple[dict, list[dict]]:
    """Raw API tour -> (hotel row, offer rows with embedded snapshot fields).

    pax composition comes from the search query, not the response:
    the API echoes pax back as an empty list.
    """
    ages = ages_str(children_ages)
    h = tour["hotel"]
    loc = h.get("location") or {}
    region = (loc.get("region") or [{}])[0]
    media = h.get("media") or []
    photo = next((m["url"] for m in media if m.get("type") == "image"), None)

    hotel = {
        "source": SOURCE_NAME,
        "source_hotel_id": str(h["id"]),
        "name": h.get("name", ""),
        "category": (h.get("category") or {}).get("category"),
        "country_id": region.get("id"),
        "country_name": region.get("name") or loc.get("country"),
        "city_name": loc.get("city"),
        "latitude": _to_float(loc.get("latitude")),
        "longitude": _to_float(loc.get("longitude")),
        "photo_url": photo,
    }

    offers = []
    for o in tour.get("offers", []):
        frm = o.get("from") or {}
        room = (o.get("rooms") or [{}])[0]
        board = o.get("board") or {}
        price_block = o.get("price") or {}
        total = price_block.get("total_price") or {}
        avg = price_block.get("average_history_price")
        if isinstance(avg, dict):
            avg = avg.get("price")
        price_cents = to_cents(total.get("price"))
        if price_cents is None:
            continue
        offers.append({
            "source": SOURCE_NAME,
            "source_hotel_id": hotel["source_hotel_id"],
            "origin_id": str(frm.get("id") or ""),
            "origin_name": frm.get("name"),
            "date_start": o["date_start"],
            "date_end": o.get("date_end"),
            "nights": (o.get("stay") or {}).get("stay"),
            "board_code": board.get("board_type") or board.get("code") or "",
            "board_name": board.get("name"),
            "room_code": str(room.get("code") or ""),
            "room_name": room.get("name"),
            "room_placement": room.get("placement") or "",
            "pax_adl": pax_adl,
            "pax_chd": len(children_ages) if children_ages else 0,
            "children_ages": ages,
            "link": PUBLIC_HOTEL_URL.format(lang=lang, hotel_id=h["id"]),
            # snapshot fields
            "price_cents": price_cents,
            "currency": (total.get("currency") or {}).get("code", "EUR"),
            "availability": _text_or_none(o.get("availability")),
            "stop_sale": _text_or_none(o.get("stop_sale")),
            "operator_avg_price_cents": to_cents(avg),
        })
    return hotel, offers


def _to_float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _text_or_none(value) -> str | None:
    if value is None:
        return None
    return str(value)
