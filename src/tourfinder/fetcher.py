"""Fetch run: pull tours from a source and store offers + price snapshots.

Per destination: one paginated search over the whole date window (all
valid stay lengths in a single comma-list query), then a second small
pass filtered to hot tours that only flips is_hot on this run's snapshots.
"""
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

from .sources import joinup

log = logging.getLogger(__name__)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_fetch(conn: sqlite3.Connection, client: joinup.JoinUpClient,
              origin: str = joinup.RIGA_ORIGIN_ID,
              days_from: int = 1, days_till: int = 30,
              adults: int = 2, children_ages: list[int] | None = None,
              only_destinations: list[str] | None = None,
              max_pages: int | None = None, tier: str | None = None) -> dict:
    date_from = date.today() + timedelta(days=days_from)
    date_till = date.today() + timedelta(days=days_till)
    dates = f"{date_from.isoformat()}:{date_till.isoformat()}"

    params = dict(origin=origin, dates=dates, adults=adults,
                  children_ages=children_ages,
                  destinations=only_destinations, max_pages=max_pages, tier=tier)
    run_id = conn.execute(
        "INSERT INTO fetch_runs(started_at, params) VALUES (?, ?)",
        (utcnow(), json.dumps(params)),
    ).lastrowid
    conn.commit()

    offers_seen = 0
    errors: list[str] = []
    try:
        destinations = client.destinations(origin)
        if only_destinations:
            destinations = [d for d in destinations if d["id"] in only_destinations]
        log.info("run %s: %s destinations, window %s", run_id, len(destinations), dates)

        for dest in destinations:
            dest_id = dest["id"]
            try:
                stays = client.stays(origin, dest_id, dates)
                if not stays:
                    log.info("%s: no stays available, skip", dest_id)
                    continue

                # Comma lists of stays are unreliable: >4 values or a single
                # value with zero results silently empties the whole response.
                # One query per stay value is the only safe shape.
                for stay in stays:
                    stays_param = str(stay)

                    # Commit per tour so the write lock is held for
                    # milliseconds, not across the paginated HTTP fetches —
                    # otherwise a concurrent writer (web UI) is locked out for
                    # the whole destination.
                    found = 0
                    for tour in client.search_pages(origin, dest_id, dates,
                                                    stays_param, adults,
                                                    children_ages=children_ages,
                                                    max_pages=max_pages):
                        offers_seen += _store_tour(conn, tour, run_id, adults,
                                                   children_ages, client.lang,
                                                   is_hot=False)
                        conn.commit()
                        found += 1
                    if not found:
                        log.info("%s stays=%s: no tours", dest_id, stays_param)
                        continue

                    for tour in client.search_pages(origin, dest_id, dates,
                                                    stays_param, adults,
                                                    children_ages=children_ages,
                                                    tour_types=joinup.HOT_TOUR_TYPE,
                                                    max_pages=max_pages):
                        _store_tour(conn, tour, run_id, adults, children_ages,
                                    client.lang, is_hot=True)
                        conn.commit()
                log.info("%s done, offers so far: %s, requests: %s",
                         dest_id, offers_seen, client.requests_made)
            except joinup.JoinUpBlockedError:
                raise
            except Exception as exc:  # one bad destination must not kill the run
                log.exception("destination %s failed", dest_id)
                errors.append(f"{dest_id}: {exc}")
    except joinup.JoinUpBlockedError as exc:
        errors.append(str(exc))
        log.error("source blocked us, aborting run: %s", exc)

    conn.execute(
        "UPDATE fetch_runs SET finished_at=?, requests_made=?, offers_seen=?, errors=? WHERE id=?",
        (utcnow(), client.requests_made, offers_seen,
         json.dumps(errors) if errors else None, run_id),
    )
    conn.commit()
    return {"run_id": run_id, "offers_seen": offers_seen,
            "requests_made": client.requests_made, "errors": errors}


def _store_tour(conn: sqlite3.Connection, tour: dict, run_id: int,
                adults: int, children_ages: list[int] | None,
                lang: str, is_hot: bool) -> int:
    hotel, offers = joinup.normalize(tour, pax_adl=adults,
                                     children_ages=children_ages, lang=lang)
    now = utcnow()

    conn.execute(
        """INSERT INTO hotels(source, source_hotel_id, name, category, country_id,
                              country_name, city_name, latitude, longitude, photo_url)
           VALUES (:source, :source_hotel_id, :name, :category, :country_id,
                   :country_name, :city_name, :latitude, :longitude, :photo_url)
           ON CONFLICT(source, source_hotel_id) DO UPDATE SET
               name=excluded.name, category=excluded.category,
               country_id=excluded.country_id, country_name=excluded.country_name,
               city_name=excluded.city_name, latitude=excluded.latitude,
               longitude=excluded.longitude, photo_url=excluded.photo_url""",
        hotel,
    )

    stored = 0
    for o in offers:
        if not o["nights"] or not o["origin_id"]:
            continue
        o["now"] = now
        cur = conn.execute(
            """INSERT INTO offers(source, source_hotel_id, origin_id, origin_name,
                                  date_start, date_end, nights, board_code, board_name,
                                  room_code, room_name, room_placement,
                                  pax_adl, pax_chd, children_ages, link,
                                  first_seen_at, last_seen_at)
               VALUES (:source, :source_hotel_id, :origin_id, :origin_name,
                       :date_start, :date_end, :nights, :board_code, :board_name,
                       :room_code, :room_name, :room_placement,
                       :pax_adl, :pax_chd, :children_ages, :link, :now, :now)
               ON CONFLICT DO UPDATE SET last_seen_at=:now, link=:link
               RETURNING id""",
            o,
        )
        offer_id = cur.fetchone()[0]

        existing = conn.execute(
            "SELECT id FROM price_snapshots WHERE offer_id=? AND run_id=?",
            (offer_id, run_id),
        ).fetchone()
        if existing:
            if is_hot:
                conn.execute("UPDATE price_snapshots SET is_hot=1 WHERE id=?",
                             (existing["id"],))
        else:
            conn.execute(
                """INSERT INTO price_snapshots(offer_id, run_id, fetched_at, price_cents,
                                               currency, is_hot, availability, stop_sale,
                                               operator_avg_price_cents)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (offer_id, run_id, now, o["price_cents"], o["currency"],
                 int(is_hot), o["availability"], o["stop_sale"],
                 o["operator_avg_price_cents"]),
            )
            stored += 1
    return stored
