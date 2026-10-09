"""Fetch run: pull tours from a source and store offers + price snapshots.

Per destination: one paginated search over the whole date window (all
valid stay lengths in a single comma-list query), then a second small
pass filtered to hot tours that only flips is_hot on this run's snapshots.
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

from .sources import joinup, waavo

log = logging.getLogger(__name__)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CollectionBudgetExceeded(RuntimeError):
    """Partial work remains stored, but must not count as fresh coverage."""


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise CollectionBudgetExceeded("partial: collection time budget exhausted")


def collector_owner() -> dict:
    """GitHub concurrency makes earlier runs of this same workflow inactive."""
    return {key: os.environ[key] for key in
            ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW", "GITHUB_REPOSITORY")
            if os.environ.get(key)}


def _start_run(conn, source, tier, pax_spec, params):
    params = {**params, "source": source, "collector_owner": collector_owner()}
    run_id = conn.execute(
        "INSERT INTO fetch_runs(started_at, tier, pax_spec, params) "
        "VALUES (:now, :tier, :pax, :params) RETURNING id",
        {"now": utcnow(), "tier": tier, "pax": pax_spec,
         "params": json.dumps(params)},
    ).fetchone()["id"]
    conn.commit()
    return run_id


def _finish_run(conn, run_id, client, offers_seen, errors, *, writer=None):
    conn.execute(
        "UPDATE fetch_runs SET finished_at=:now, requests_made=:req, "
        "offers_seen=:seen, errors=:errors WHERE id=:id",
        {"now": utcnow(), "req": client.requests_made, "seen": offers_seen,
         "errors": json.dumps(errors) if errors else None, "id": run_id},
    )
    conn.commit()
    if writer is not None:
        log.info("run %s write batches: batch_size=%s flushes=%s committed=%s flush_seconds=%.3f",
                 run_id, writer.batch_size, writer.flush_count, writer.flush_committed,
                 writer.flush_seconds)
    return {"run_id": run_id, "offers_seen": offers_seen,
            "requests_made": client.requests_made, "errors": errors,
            "completed": not errors}


def run_fetch(conn, client: joinup.JoinUpClient,
              origin: str = joinup.RIGA_ORIGIN_ID,
              days_from: int = 1, days_till: int = 30,
              adults: int = 2, children_ages: list[int] | None = None,
              only_destinations: list[str] | None = None,
              max_pages: int | None = None, tier: str | None = None,
              pax_spec: str | None = None, deadline: float | None = None) -> dict:
    date_from = date.today() + timedelta(days=days_from)
    date_till = date.today() + timedelta(days=days_till)
    dates = f"{date_from.isoformat()}:{date_till.isoformat()}"

    params = dict(origin=origin, dates=dates, adults=adults,
                  children_ages=children_ages,
                  destinations=only_destinations, max_pages=max_pages, tier=tier)
    run_id = _start_run(conn, "joinup", tier, pax_spec, params)
    writer = _BatchWriter(conn, run_id)
    errors: list[str] = []
    try:
        client.deadline = deadline
        _check_deadline(deadline)
        destinations = client.destinations(origin)
        if only_destinations:
            destinations = [d for d in destinations if d["id"] in only_destinations]
        log.info("run %s: %s destinations, window %s", run_id, len(destinations), dates)

        for dest in destinations:
            _check_deadline(deadline)
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
                    _check_deadline(deadline)
                    stays_param = str(stay)

                    # Network pagination happens outside write transactions;
                    # each small normalized batch is committed independently.
                    found = 0
                    for tour in client.search_pages(origin, dest_id, dates,
                                                    stays_param, adults,
                                                    children_ages=children_ages,
                                                    max_pages=max_pages):
                        _check_deadline(deadline)
                        hotel, offers = joinup.normalize(tour, adults, children_ages,
                                                         client.lang)
                        for offer in offers:
                            _check_deadline(deadline)
                            offer.setdefault("operator", "joinup")
                            writer.add(hotel, offer)
                        found += 1
                    writer.flush()
                    if not found:
                        log.info("%s stays=%s: no tours", dest_id, stays_param)
                        continue

                    _check_deadline(deadline)
                    for tour in client.search_pages(origin, dest_id, dates,
                                                    stays_param, adults,
                                                    children_ages=children_ages,
                                                    tour_types=joinup.HOT_TOUR_TYPE,
                                                    max_pages=max_pages):
                        _check_deadline(deadline)
                        hotel, offers = joinup.normalize(tour, adults, children_ages,
                                                         client.lang)
                        for offer in offers:
                            _check_deadline(deadline)
                            offer.setdefault("operator", "joinup")
                            writer.add(hotel, offer, is_hot=True)
                    writer.flush()
                log.info("%s done, offers so far: %s, requests: %s",
                         dest_id, writer.offers_seen, client.requests_made)
            except (joinup.JoinUpBlockedError, joinup.JoinUpDeadlineError, CollectionBudgetExceeded):
                raise
            except Exception as exc:  # one bad destination must not kill the run
                conn.rollback()
                log.error("destination %s failed: %s", dest_id, type(exc).__name__)
                errors.append(f"{dest_id}: {type(exc).__name__}")
    except (joinup.JoinUpBlockedError, joinup.JoinUpDeadlineError, CollectionBudgetExceeded) as exc:
        errors.append(str(exc))
        log.error("run stopped: %s", exc)
    except Exception as exc:
        conn.rollback()
        errors.append(type(exc).__name__)
        log.error("joinup run failed: %s", type(exc).__name__)
    except BaseException:
        conn.rollback()
        _finish_run(conn, run_id, client, writer.offers_seen, ["interrupted"], writer=writer)
        raise
    try:
        writer.flush()
    except Exception as exc:
        errors.append(type(exc).__name__)
    if max_pages:
        errors.append("partial: max_pages limits source coverage")
    return _finish_run(conn, run_id, client, writer.offers_seen, errors, writer=writer)


def run_waavo_fetch(conn, client: waavo.WaavoClient,
                    days_from: int = 1, days_till: int = 30,
                    adults: int = 2, children_ages: list[int] | None = None,
                    tier: str | None = None, pax_spec: str | None = None,
                    max_pages: int | None = None,
                    deadline: float | None = None) -> dict:
    """One Waavo run: paginate the aggregator search over a date window,
    skip Join Up (collected directly), store offers + TripAdvisor reviews."""
    date_from = (date.today() + timedelta(days=days_from)).isoformat()
    date_till = (date.today() + timedelta(days=days_till)).isoformat()

    duration_from, duration_till = 2, 21
    params = dict(source="waavo", dateFrom=date_from, dateTo=date_till,
                  adults=adults, children_ages=children_ages, tier=tier,
                  departureAirport=waavo.RIGA_AIRPORT,
                  durationFrom=duration_from, durationTo=duration_till,
                  max_pages=max_pages)
    run_id = _start_run(conn, "waavo", tier, pax_spec, params)
    writer = _BatchWriter(conn, run_id)
    errors: list[str] = []
    try:
        client.deadline = deadline
        _check_deadline(deadline)
        for raw in client.search_pages(date_from, date_till, adults,
                                       children_ages=children_ages,
                                       duration_from=duration_from,
                                       duration_till=duration_till,
                                       max_pages=max_pages):
            _check_deadline(deadline)
            if waavo.should_skip(raw):
                continue
            hotel, offer, review = waavo.normalize(raw, adults, children_ages)
            if not offer["source_hotel_id"] or not offer["date_start"]:
                continue
            writer.add(hotel, offer, review=review)
    except (waavo.WaavoBlockedError, waavo.WaavoDeadlineError, CollectionBudgetExceeded) as exc:
        errors.append(str(exc))
        log.error("waavo run stopped: %s", exc)
    except Exception as exc:
        conn.rollback()
        log.error("waavo run failed: %s", type(exc).__name__)
        errors.append(type(exc).__name__)
    except BaseException:
        conn.rollback()
        _finish_run(conn, run_id, client, writer.offers_seen, ["interrupted"], writer=writer)
        raise
    try:
        writer.flush()
    except Exception as exc:
        errors.append(type(exc).__name__)
    if max_pages:
        errors.append("partial: max_pages limits source coverage")
    return _finish_run(conn, run_id, client, writer.offers_seen, errors, writer=writer)


def prune_snapshots(conn) -> int:
    """Collapse constant runs of snapshots to their first and last point.

    A snapshot is dropped when its neighbours (same offer, time order) carry
    identical price / hot flag / availability / operator average — the trend
    line through the survivors is unchanged, so history loses nothing while
    the table stops growing linearly with polling frequency (Supabase free
    tier is 500 MB).
    """
    result = conn.execute("""
        DELETE FROM price_snapshots WHERE id IN (
            SELECT id FROM (
                SELECT id, price_cents, is_hot,
                       COALESCE(availability, '') AS a,
                       COALESCE(operator_avg_price_cents, -1) AS oa,
                       LAG(price_cents)  OVER w AS pp,
                       LEAD(price_cents) OVER w AS np,
                       LAG(is_hot)       OVER w AS ph,
                       LEAD(is_hot)      OVER w AS nh,
                       COALESCE(LAG(availability)  OVER w, '') AS pa,
                       COALESCE(LEAD(availability) OVER w, '') AS na,
                       COALESCE(LAG(operator_avg_price_cents)  OVER w, -1) AS poa,
                       COALESCE(LEAD(operator_avg_price_cents) OVER w, -1) AS noa
                FROM price_snapshots
                WINDOW w AS (PARTITION BY offer_id ORDER BY fetched_at, id)
            ) t
            WHERE pp = price_cents AND np = price_cents
              AND ph = is_hot AND nh = is_hot
              AND pa = a AND na = a
              AND poa = oa AND noa = oa
        )""")
    conn.commit()
    return result.rowcount


_OFFER_COLS = ("source", "source_hotel_id", "origin_id", "origin_name",
               "date_start", "date_end", "nights", "board_code", "board_name",
               "room_code", "room_name", "room_placement",
               "pax_adl", "pax_chd", "children_ages", "operator", "link")

_IDENTITY_COLS = ("source", "source_hotel_id", "origin_id", "date_start",
                  "nights", "board_code", "room_code", "room_placement",
                  "pax_adl", "pax_chd", "children_ages")
_HOTEL_COLS = ("source", "source_hotel_id", "name", "category", "country_id",
               "country_name", "city_name", "latitude", "longitude", "photo_url")
_REVIEW_COLS = ("source", "source_hotel_id", "platform", "rating", "rating_scale",
                "reviews_count", "summary", "external_id", "url", "matched_name",
                "match_status", "fetched_at")
_SNAPSHOT_COLS = ("offer_id", "run_id", "fetched_at", "price_cents", "currency",
                  "is_hot", "availability", "stop_sale", "operator_avg_price_cents")


def _identity(offer):
    # API fields sometimes contain numeric codes; TEXT columns return strings.
    return tuple("" if offer.get(key) is None else str(offer[key])
                 for key in _IDENTITY_COLS)


def _insert_many(conn, table, columns, rows, *, conflict=(), update=(), returning=()):
    """Small portable multi-VALUES writes. Identifiers are internal constants."""
    if not rows:
        return []
    params = {}
    values = []
    for index, row in enumerate(rows):
        marks = []
        for column in columns:
            key = f"r{index}_{column}"
            params[key] = row.get(column)
            marks.append(":" + key)
        values.append("(" + ",".join(marks) + ")")
    statement = f"INSERT INTO {table} ({','.join(columns)}) VALUES {','.join(values)}"
    if conflict:
        statement += (f" ON CONFLICT ({','.join(conflict)}) DO UPDATE SET " +
                      ",".join(f"{column}=excluded.{column}" for column in update))
    if returning:
        statement += " RETURNING " + ",".join(returning)
    result = conn.execute(statement, params)
    return result.fetchall() if returning else []


def _store_batch(conn, entries, run_id):
    """Preserve one snapshot per offer/run and the existing offer identity.

    The first observed price wins, as in _store_offer; repeated rows update the
    link and last_seen_at, and the hot pass may set the existing snapshot's flag.
    Chunk deduplication is required by PostgreSQL ON CONFLICT multi-row writes.
    """
    hotels, offers, first_prices, hot_keys, reviews = {}, {}, {}, set(), {}
    for hotel, offer, review, is_hot, observed_at in entries:
        hotels[(hotel["source"], hotel["source_hotel_id"])] = hotel
        if review:
            reviews[(review["source"], review["source_hotel_id"], review["platform"])] = {
                **review, "fetched_at": observed_at}
        if not offer.get("nights") or not offer.get("origin_id") or offer.get("price_cents") is None:
            continue
        key = _identity(offer)
        first_prices.setdefault(key, (offer, observed_at))
        offers[key] = {**offer, "first_seen_at": first_prices[key][1],
                       "last_seen_at": observed_at}
        if is_hot:
            hot_keys.add(key)
    _insert_many(conn, "hotels", _HOTEL_COLS, list(hotels.values()),
                 conflict=("source", "source_hotel_id"), update=_HOTEL_COLS[2:])
    stored = _insert_many(
        conn, "offers", _OFFER_COLS + ("first_seen_at", "last_seen_at"),
        list(offers.values()), conflict=_IDENTITY_COLS,
        update=("last_seen_at", "link", "operator"), returning=("id",) + _IDENTITY_COLS)
    snapshots = []
    if stored:
        id_params = {f"o{index}": row["id"] for index, row in enumerate(stored)}
        marks = ",".join(":" + key for key in id_params)
        existing = {row["offer_id"] for row in conn.execute(
            f"SELECT offer_id FROM price_snapshots WHERE run_id=:run AND offer_id IN ({marks})",
            {**id_params, "run": run_id}).fetchall()}
        hot_ids = []
        for row in stored:
            key = _identity(row)
            offer, observed_at = first_prices[key]
            if row["id"] in existing:
                if key in hot_keys:
                    hot_ids.append(row["id"])
                continue
            snapshots.append({**offer, "offer_id": row["id"], "run_id": run_id,
                              "fetched_at": observed_at, "is_hot": int(key in hot_keys)})
        if hot_ids:
            hot_params = {f"h{index}": value for index, value in enumerate(hot_ids)}
            hot_marks = ",".join(":" + key for key in hot_params)
            conn.execute(f"UPDATE price_snapshots SET is_hot=1 WHERE run_id=:run "
                         f"AND offer_id IN ({hot_marks})", {**hot_params, "run": run_id})
        _insert_many(conn, "price_snapshots", _SNAPSHOT_COLS, snapshots)
    _insert_many(conn, "hotel_reviews", _REVIEW_COLS, list(reviews.values()),
                 conflict=("source", "source_hotel_id", "platform"),
                 update=("rating", "reviews_count", "matched_name", "fetched_at"))
    return len(snapshots)


class _BatchWriter:
    # 40 * 19 offer columns stays below SQLite's legacy 999 parameter limit.
    SQLITE_BATCH_SIZE = 40
    # Keep remote PostgreSQL transactions modest while amortizing round trips.
    # The largest statement binds 200 * 19 = 3800 values.
    POSTGRES_BATCH_SIZE = 200

    def __init__(self, conn, run_id):
        self.conn, self.run_id = conn, run_id
        self.batch_size = (self.POSTGRES_BATCH_SIZE if conn.dialect == "postgresql"
                           else self.SQLITE_BATCH_SIZE)
        self.entries = []
        self.offers_seen = 0
        self.flush_count = 0
        self.flush_committed = 0
        self.flush_seconds = 0.0

    def add(self, hotel, offer, *, review=None, is_hot=False):
        self.entries.append((hotel, offer, review, is_hot, utcnow()))
        if len(self.entries) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self.entries:
            return
        entries, self.entries = self.entries, []
        started = time.monotonic()
        self.flush_count += 1
        try:
            count = _store_batch(self.conn, entries, self.run_id)
            self.conn.commit()
            self.flush_committed += 1
        except Exception:
            self.conn.rollback()
            raise
        finally:
            # Includes statement/commit/rollback work, never source requests.
            self.flush_seconds += max(0.0, time.monotonic() - started)
        self.offers_seen += count


def _upsert_hotel(conn, hotel: dict) -> None:
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


def _store_offer(conn, o: dict, run_id: int, now: str, is_hot: bool) -> bool:
    """Upsert one offer + its price snapshot for this run. Returns True if a
    new snapshot was stored."""
    if not o["nights"] or not o["origin_id"] or o["price_cents"] is None:
        return False
    op = {k: o.get(k) for k in _OFFER_COLS}
    op["now"] = now
    offer_id = conn.execute(
        """INSERT INTO offers(source, source_hotel_id, origin_id, origin_name,
                              date_start, date_end, nights, board_code, board_name,
                              room_code, room_name, room_placement,
                              pax_adl, pax_chd, children_ages, operator, link,
                              first_seen_at, last_seen_at)
           VALUES (:source, :source_hotel_id, :origin_id, :origin_name,
                   :date_start, :date_end, :nights, :board_code, :board_name,
                   :room_code, :room_name, :room_placement,
                   :pax_adl, :pax_chd, :children_ages, :operator, :link, :now, :now)
           ON CONFLICT (source, source_hotel_id, origin_id, date_start, nights,
                        board_code, room_code, room_placement,
                        pax_adl, pax_chd, children_ages)
           DO UPDATE SET last_seen_at=excluded.last_seen_at, link=excluded.link,
                         operator=excluded.operator
           RETURNING id""",
        op,
    ).fetchone()["id"]

    existing = conn.execute(
        "SELECT id FROM price_snapshots WHERE offer_id=:o AND run_id=:r",
        {"o": offer_id, "r": run_id},
    ).fetchone()
    if existing:
        if is_hot:
            conn.execute("UPDATE price_snapshots SET is_hot=1 WHERE id=:id",
                         {"id": existing["id"]})
        return False
    conn.execute(
        """INSERT INTO price_snapshots(offer_id, run_id, fetched_at, price_cents,
                                       currency, is_hot, availability, stop_sale,
                                       operator_avg_price_cents)
           VALUES (:offer_id, :run_id, :now, :price, :currency, :is_hot,
                   :availability, :stop_sale, :op_avg)""",
        {"offer_id": offer_id, "run_id": run_id, "now": now,
         "price": o["price_cents"], "currency": o["currency"],
         "is_hot": int(is_hot), "availability": o["availability"],
         "stop_sale": o["stop_sale"], "op_avg": o["operator_avg_price_cents"]},
    )
    return True


def _upsert_review(conn, review: dict, now: str) -> None:
    review = {**review, "fetched_at": now}
    conn.execute(
        """INSERT INTO hotel_reviews(source, source_hotel_id, platform, rating,
               rating_scale, reviews_count, summary, external_id, url,
               matched_name, match_status, fetched_at)
           VALUES (:source,:source_hotel_id,:platform,:rating,:rating_scale,
               :reviews_count,:summary,:external_id,:url,:matched_name,
               :match_status,:fetched_at)
           ON CONFLICT(source, source_hotel_id, platform) DO UPDATE SET
               rating=excluded.rating, reviews_count=excluded.reviews_count,
               matched_name=excluded.matched_name, fetched_at=excluded.fetched_at""",
        review,
    )


def _store_tour(conn, tour: dict, run_id: int,
                adults: int, children_ages: list[int] | None,
                lang: str, is_hot: bool) -> int:
    hotel, offers = joinup.normalize(tour, pax_adl=adults,
                                     children_ages=children_ages, lang=lang)
    now = utcnow()
    _upsert_hotel(conn, hotel)
    stored = 0
    for o in offers:
        o.setdefault("operator", "joinup")
        if _store_offer(conn, o, run_id, now, is_hot):
            stored += 1
    return stored
