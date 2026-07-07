"""Enrich hotels with guest reviews and compute the stars-vs-guests gap."""
import logging
import sqlite3
from datetime import datetime, timezone

from .sources.reviews import ReviewProvider

log = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hotels_needing_reviews(conn: sqlite3.Connection, platform: str,
                           max_age_days: int = 30, limit: int | None = None,
                           only_searchable: bool = True) -> list[sqlite3.Row]:
    """Hotels with no review row for this platform, or a stale one.

    only_searchable keeps to hotels currently reachable in search (they have
    at least one offer), so we don't spend lookups on dead catalog entries.
    """
    sql = f"""
        SELECT h.* FROM hotels h
        WHERE {"EXISTS (SELECT 1 FROM offers o WHERE o.source=h.source "
                       "AND o.source_hotel_id=h.source_hotel_id) AND" if only_searchable else ""}
              NOT EXISTS (
            SELECT 1 FROM hotel_reviews r
            WHERE r.source = h.source AND r.source_hotel_id = h.source_hotel_id
              AND r.platform = :platform
              AND r.fetched_at > datetime('now', :cutoff)
        )
        ORDER BY h.name
    """
    params = {"platform": platform, "cutoff": f"-{max_age_days} days"}
    rows = conn.execute(sql, params).fetchall()
    return rows[:limit] if limit else rows


def enrich(conn: sqlite3.Connection, provider: ReviewProvider,
           max_age_days: int = 30, limit: int | None = None) -> dict:
    if not provider.available():
        return {"available": False, "checked": 0, "stored": 0,
                "reason": "provider has no credentials"}

    hotels = hotels_needing_reviews(conn, provider.platform, max_age_days, limit)
    now = _utcnow()
    checked = stored = errors = 0
    for h in hotels:
        try:
            res = provider.lookup(name=h["name"], city=h["city_name"],
                                  country=h["country_name"],
                                  lat=h["latitude"], lon=h["longitude"])
        except Exception:
            log.exception("review lookup failed for %s", h["name"])
            errors += 1
            continue
        checked += 1
        if res is None:
            break  # provider went unavailable mid-run
        res = {"rating": None, "rating_scale": 5, "reviews_count": None,
               "summary": None, "external_id": None, "url": None,
               "matched_name": None, "match_status": "ok", **res}
        conn.execute(
            """INSERT INTO hotel_reviews(source, source_hotel_id, platform, rating,
                   rating_scale, reviews_count, summary, external_id, url,
                   matched_name, match_status, fetched_at)
               VALUES (:source,:source_hotel_id,:platform,:rating,:rating_scale,
                   :reviews_count,:summary,:external_id,:url,:matched_name,
                   :match_status,:fetched_at)
               ON CONFLICT(source, source_hotel_id, platform) DO UPDATE SET
                   rating=excluded.rating, rating_scale=excluded.rating_scale,
                   reviews_count=excluded.reviews_count, summary=excluded.summary,
                   external_id=excluded.external_id, url=excluded.url,
                   matched_name=excluded.matched_name,
                   match_status=excluded.match_status, fetched_at=excluded.fetched_at""",
            {"source": h["source"], "source_hotel_id": h["source_hotel_id"],
             "platform": provider.platform, "fetched_at": now, **res},
        )
        conn.commit()
        stored += 1
    return {"available": True, "checked": checked, "stored": stored,
            "errors": errors, "candidates": len(hotels)}


def star_gap(category, rating, rating_scale) -> float | None:
    """Guest rating minus official stars, both on a 0..5 scale. Negative =
    guests rate below the star class (overrated); positive = underrated."""
    if category in (None, "") or rating in (None, ""):
        return None
    try:
        stars = float(category)
        guest5 = float(rating) * 5.0 / float(rating_scale or 5)
    except (TypeError, ValueError):
        return None
    return round(guest5 - stars, 2)
