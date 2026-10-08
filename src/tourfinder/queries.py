"""Read queries over collected offers, shared by the UI and subscriptions.

Filter offers before looking up their latest price. Materialized CTEs require
PostgreSQL 12+ or SQLite 3.35+, supported by the application's runtimes.
"""
from datetime import datetime, timedelta, timezone

from . import meals


FRESH_HOURS = 48


def _fresh_cutoff(hours: int = FRESH_HOURS) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _norm_ages(children_ages: str | None) -> str:
    """Canonical sorted child ages, matching the collector's offer identity."""
    if not children_ages:
        return ""
    ages = [int(a) for a in str(children_ages).split(",") if a.strip()]
    return ",".join(str(a) for a in sorted(ages))


def available_compositions(conn) -> list[dict]:
    """Party compositions we actually hold offers for."""
    rows = conn.execute(
        """SELECT pax_adl, pax_chd, children_ages, count(*) AS offers
           FROM offers GROUP BY pax_adl, pax_chd, children_ages
           ORDER BY pax_adl, pax_chd, children_ages"""
    ).fetchall()
    return [dict(r) for r in rows]


# Only these server-owned expressions are interpolated into SQL.
SORTS = {
    "price": "l.price_cents",
    "price_per_night": "l.price_cents * 1.0 / o.nights",
}


def _build_filters(*, date_from: str, date_till: str, adults: int,
                   children_ages: str | None, nights_min: int, nights_max: int,
                   budget_max: int | None, boards: str | None,
                   countries: str | None, only_hot: bool,
                   stars_min: int | None = None,
                   board_categories: str | None = None,
                   hotel_id: str | None = None,
                   source: str | None = None,
                   limit: int = 100) -> tuple[list[str], list[str], dict]:
    """Separate offer/hotel filters from filters on the latest snapshot.

    Budget and hot filters must not affect WHICH snapshot is the latest.
    LIMIT belongs after every filter, so early candidates cannot starve later
    matching offers.
    """
    where = ["o.date_start BETWEEN :date_from AND :date_till",
             "o.nights BETWEEN :nights_min AND :nights_max",
             "o.pax_adl = :adults",
             "o.children_ages = :ages",
             "o.last_seen_at >= :fresh_cutoff"]
    snapshot_where = []
    params = {"date_from": date_from, "date_till": date_till,
              "nights_min": nights_min, "nights_max": nights_max,
              "adults": adults, "ages": _norm_ages(children_ages),
              "limit": max(0, min(limit, 500)), "fresh_cutoff": _fresh_cutoff()}
    if budget_max:
        snapshot_where.append("l.price_cents <= :budget_cents")
        params["budget_cents"] = budget_max * 100
    if boards:
        codes = [b.strip() for b in boards.split(",") if b.strip()]
        if codes:
            marks = ",".join(f":b{i}" for i in range(len(codes)))
            params.update({f"b{i}": c for i, c in enumerate(codes)})
            where.append(f"o.board_code IN ({marks})")
    categories = meals.normalize_categories(board_categories)
    if categories:
        codes = categories.split(",")
        marks = ",".join(f":meal{i}" for i in range(len(codes)))
        params.update({f"meal{i}": value for i, value in enumerate(codes)})
        where.append(f"({meals.category_sql()}) IN ({marks})")
    if countries:
        ids = [c.strip() for c in countries.split(",") if c.strip()]
        if ids:
            marks = ",".join(f":c{i}" for i in range(len(ids)))
            params.update({f"c{i}": c for i, c in enumerate(ids)})
            where.append(f"h.country_id IN ({marks})")
    if only_hot:
        snapshot_where.append("l.is_hot = 1")
    if stars_min:
        # Categories include '4+', 'HV1', etc.; only cast a leading digit.
        where.append(
            "CASE WHEN substr(h.category, 1, 1) BETWEEN '0' AND '9' "
            "THEN CAST(substr(h.category, 1, 1) AS INTEGER) END >= :stars_min")
        params["stars_min"] = stars_min
    if hotel_id:
        where.append("o.source_hotel_id = :hotel_id")
        params["hotel_id"] = str(hotel_id)
    if source:
        where.append("o.source = :source")
        params["source"] = source
    return where, snapshot_where, params


def _candidates_sql(where: list[str]) -> str:
    return f"""candidates AS MATERIALIZED (
        SELECT o.*, h.name AS hotel_name, h.category, h.country_name,
               h.city_name, h.photo_url, {meals.category_sql()} AS board_category
        FROM offers o
        JOIN hotels h ON h.source = o.source AND h.source_hotel_id = o.source_hotel_id
        WHERE {' AND '.join(where)}
    )"""


def _matched_sql(snapshot_where: list[str], sort_expr: str) -> str:
    # (offer_id, fetched_at, id) supports one reverse index seek per candidate.
    # id resolves ties between observations recorded in the same second.
    return f"""
        SELECT o.id AS offer_id, o.source, o.source_hotel_id, o.date_start,
               o.date_end, o.nights, o.board_code, o.board_name, o.board_category,
               o.room_code, o.room_name, o.room_placement, o.last_seen_at,
               o.link, o.origin_name, o.pax_adl, o.pax_chd, o.children_ages,
               o.operator, o.hotel_name, o.category, o.country_name,
               o.city_name, o.photo_url,
               l.price_cents, l.currency, l.is_hot, l.fetched_at,
               l.availability, l.stop_sale, l.operator_avg_price_cents,
               CAST(l.price_cents * 1.0 / o.nights AS INTEGER) AS price_per_night_cents,
               {sort_expr} AS sort_key
        FROM candidates o
        JOIN price_snapshots l ON l.id = (
            SELECT ps.id FROM price_snapshots ps WHERE ps.offer_id = o.id
            ORDER BY ps.fetched_at DESC, ps.id DESC LIMIT 1
        )
        WHERE {' AND '.join(snapshot_where) or '1=1'}
    """


_REVIEW_COLUMNS = """
    br.platform AS review_platform, br.rating AS review_rating,
    br.rating_scale AS review_scale, br.reviews_count AS review_count,
    br.url AS review_url, br.match_status AS review_match_status
"""
_REVIEW_JOIN = """
    LEFT JOIN hotel_reviews br ON br.id = (
        SELECT r.id FROM hotel_reviews r
        WHERE r.source = m.source AND r.source_hotel_id = m.source_hotel_id
          AND r.rating IS NOT NULL AND r.match_status = 'ok'
        ORDER BY (r.reviews_count IS NULL), r.reviews_count DESC, r.id DESC
        LIMIT 1
    )
"""


def search_offers(conn, *, date_from: str, date_till: str,
                  adults: int = 2, children_ages: str | None = None,
                  nights_min: int = 1, nights_max: int = 30,
                  budget_max: int | None = None, boards: str | None = None,
                  board_categories: str | None = None,
                  countries: str | None = None, only_hot: bool = False,
                  stars_min: int | None = None, hotel_id: str | None = None,
                  source: str | None = None,
                  sort: str = "price", limit: int = 100,
                  offset: int = 0) -> list[dict]:
    """Offers whose latest snapshot matches every filter, cheapest first.

    History statistics and reviews are read only for the final result page.
    budget_max is expressed in whole currency units.
    """
    where, snapshot_where, params = _build_filters(
        date_from=date_from, date_till=date_till, adults=adults,
        children_ages=children_ages, nights_min=nights_min,
        nights_max=nights_max, budget_max=budget_max, boards=boards, board_categories=board_categories,
        countries=countries, only_hot=only_hot, stars_min=stars_min,
        hotel_id=hotel_id, source=source, limit=limit)
    sort_expr = SORTS.get(sort, SORTS["price"])
    params["offset"] = max(0, offset)
    query = f"""
        WITH {_candidates_sql(where)},
        selected AS MATERIALIZED (
            {_matched_sql(snapshot_where, sort_expr)}
            ORDER BY sort_key ASC, offer_id ASC LIMIT :limit OFFSET :offset
        )
        SELECT m.*, {_REVIEW_COLUMNS},
               (SELECT count(*) FROM price_snapshots ps WHERE ps.offer_id = m.offer_id) AS snapshots_count,
               (SELECT CAST(avg(price_cents) AS REAL) FROM price_snapshots ps WHERE ps.offer_id = m.offer_id) AS avg_seen_cents,
               (SELECT min(price_cents) FROM price_snapshots ps WHERE ps.offer_id = m.offer_id) AS min_seen_cents,
               (SELECT max(price_cents) FROM price_snapshots ps WHERE ps.offer_id = m.offer_id) AS max_seen_cents
        FROM selected m {_REVIEW_JOIN}
        ORDER BY m.sort_key ASC, m.offer_id ASC
    """
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    for row in rows:
        row.pop("sort_key")
    return meals.add_labels(rows)


def search_hotels_grouped(conn, *, sort: str = "price", **filters) -> list[dict]:
    """Cheapest variant per source/hotel, with all matching variant statistics.

    LIMIT applies to hotels after ranking, never to their constituent offers.
    """
    where, snapshot_where, params = _build_filters(**filters)
    sort_expr = SORTS.get(sort, SORTS["price"])
    query = f"""
        WITH {_candidates_sql(where)},
        matched AS (
            {_matched_sql(snapshot_where, sort_expr)}
        ),
        ranked AS (
            SELECT m.*,
                   ROW_NUMBER() OVER (PARTITION BY m.source, m.source_hotel_id
                                      ORDER BY m.sort_key ASC, m.offer_id ASC) AS hrn,
                   COUNT(*) OVER (PARTITION BY m.source, m.source_hotel_id) AS variants,
                   MIN(m.price_cents) OVER (PARTITION BY m.source, m.source_hotel_id) AS variants_min_cents,
                   MAX(m.price_cents) OVER (PARTITION BY m.source, m.source_hotel_id) AS variants_max_cents,
                   MIN(m.date_start) OVER (PARTITION BY m.source, m.source_hotel_id) AS variants_date_from,
                   MAX(m.date_start) OVER (PARTITION BY m.source, m.source_hotel_id) AS variants_date_till
            FROM matched m
        ),
        selected AS MATERIALIZED (
            SELECT * FROM ranked WHERE hrn = 1
            ORDER BY sort_key ASC, offer_id ASC LIMIT :limit
        )
        SELECT m.*, {_REVIEW_COLUMNS}
        FROM selected m {_REVIEW_JOIN}
        ORDER BY m.sort_key ASC, m.offer_id ASC
    """
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    for row in rows:
        row.pop("hrn")
        row.pop("sort_key")
    return meals.add_labels(rows)


def offer_detail(conn, offer_id: int) -> dict | None:
    """Read one exact variant, even when a previously saved offer is stale."""
    query = f"""
        WITH {_candidates_sql(['o.id = :offer_id'])},
        selected AS ({_matched_sql([], SORTS['price'])})
        SELECT m.*, {_REVIEW_COLUMNS},
               (SELECT count(*) FROM price_snapshots ps WHERE ps.offer_id=m.offer_id) AS snapshots_count,
               (SELECT CAST(avg(price_cents) AS REAL) FROM price_snapshots ps WHERE ps.offer_id=m.offer_id) AS avg_seen_cents,
               (SELECT min(price_cents) FROM price_snapshots ps WHERE ps.offer_id=m.offer_id) AS min_seen_cents,
               (SELECT max(price_cents) FROM price_snapshots ps WHERE ps.offer_id=m.offer_id) AS max_seen_cents
        FROM selected m {_REVIEW_JOIN}
    """
    result = conn.execute(query, {"offer_id": offer_id}).fetchone()
    if result is None:
        return None
    row = dict(result)
    row.pop("sort_key", None)
    row["stale"] = (not row.get("last_seen_at") or row["last_seen_at"] < _fresh_cutoff()
                    or not row.get("fetched_at") or row["fetched_at"] < _fresh_cutoff()
                    or row["date_start"] < datetime.now(timezone.utc).date().isoformat())
    return meals.add_labels([row])[0]


def price_drops(conn, *, adults: int = 2, children_ages: str | None = None,
                since: str | None = None, today: str | None = None,
                source: str | None = None,
                limit: int = 100) -> list[dict]:
    """Latest downward change of the same offer, despite repeated equal polls.

    Baseline: last different price/currency before the current flat series.
    A currency change or rebound is not a drop. since applies to the first
    observation of the lower price, not to today's repeated observation.
    """
    params = {"adults": adults, "ages": _norm_ages(children_ages),
              "limit": max(0, min(limit, 300)), "fresh_cutoff": _fresh_cutoff()}
    where = ["o.pax_adl = :adults", "o.children_ages = :ages",
             "o.last_seen_at >= :fresh_cutoff"]
    recent = []
    observed = []
    if since:
        params["since"] = since
        recent.append("cur.fetched_at >= :since")
        observed.append("first_low.fetched_at >= :since")
    if today:
        where.append("o.date_start >= :today")
        params["today"] = today
    if source:
        where.append("o.source = :source")
        params["source"] = source
    query = f"""
        WITH {_candidates_sql(where)},
        current_prices AS MATERIALIZED (
            SELECT o.*, cur.id AS snapshot_id, cur.price_cents, cur.currency,
                   cur.is_hot, cur.fetched_at, cur.stop_sale
            FROM candidates o
            JOIN price_snapshots cur ON cur.id = (
                SELECT ps.id FROM price_snapshots ps WHERE ps.offer_id = o.id
                ORDER BY ps.fetched_at DESC, ps.id DESC LIMIT 1
            )
            WHERE {' AND '.join(recent) or '1=1'}
        ),
        selected AS MATERIALIZED (
            SELECT o.id AS offer_id, o.source, o.source_hotel_id,
                   o.date_start, o.date_end, o.nights, o.board_code,
                   o.board_name, o.board_category, o.room_name, o.link, o.pax_adl, o.pax_chd,
                   o.children_ages, o.hotel_name, o.category, o.country_name,
                   o.city_name, o.photo_url, o.price_cents, o.currency,
                   o.is_hot, o.fetched_at, o.stop_sale,
                   prev.price_cents AS prev_price_cents,
                   prev.fetched_at AS prev_fetched_at,
                   first_low.fetched_at AS drop_observed_at,
                   (prev.price_cents - o.price_cents) AS drop_cents,
                   (prev.price_cents - o.price_cents) * 1.0 / prev.price_cents AS drop_fraction
            FROM current_prices o
            JOIN price_snapshots prev ON prev.id = (
                SELECT ps.id FROM price_snapshots ps WHERE ps.offer_id = o.id
                  AND (ps.fetched_at, ps.id) < (o.fetched_at, o.snapshot_id)
                  AND (ps.price_cents <> o.price_cents OR ps.currency <> o.currency)
                ORDER BY ps.fetched_at DESC, ps.id DESC LIMIT 1
            )
            JOIN price_snapshots first_low ON first_low.id = (
                SELECT ps.id FROM price_snapshots ps WHERE ps.offer_id = o.id
                  AND (ps.fetched_at, ps.id) > (prev.fetched_at, prev.id)
                ORDER BY ps.fetched_at ASC, ps.id ASC LIMIT 1
            )
            WHERE o.price_cents < prev.price_cents AND prev.price_cents > 0
              AND o.currency = prev.currency
              {' AND ' + ' AND '.join(observed) if observed else ''}
            ORDER BY drop_fraction DESC, first_low.fetched_at DESC, o.id ASC
            LIMIT :limit
        )
        SELECT m.*,
               (SELECT count(*) FROM price_snapshots ps WHERE ps.offer_id = m.offer_id) AS snapshots_count
        FROM selected m
        ORDER BY m.drop_fraction DESC, m.drop_observed_at DESC, m.offer_id ASC
    """
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    for row in rows:
        row.pop("drop_fraction")
    return meals.add_labels(rows)
