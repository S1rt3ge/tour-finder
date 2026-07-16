"""Shared read queries over collected offers, used by the web UI and by
subscription evaluation."""


def _norm_ages(children_ages: str | None) -> str:
    """Canonical children-ages string: sorted ints, comma joined. Accepts a
    raw '8,6' or '' and returns '6,8' / ''. Matches storage form."""
    if not children_ages:
        return ""
    ages = [int(a) for a in str(children_ages).split(",") if a.strip() != ""]
    return ",".join(str(a) for a in sorted(ages))


def available_compositions(conn) -> list[dict]:
    """Party compositions we actually hold offers for — drives the form and
    the empty-state hint."""
    rows = conn.execute(
        """SELECT pax_adl, pax_chd, children_ages, count(*) AS offers
           FROM offers GROUP BY pax_adl, pax_chd, children_ages
           ORDER BY pax_adl, pax_chd"""
    ).fetchall()
    return [dict(r) for r in rows]


# Whitelisted sort expressions (never interpolate user input into SQL).
SORTS = {
    "price": "l.price_cents",
    "price_per_night": "l.price_cents * 1.0 / o.nights",
}


def _build_filters(*, date_from: str, date_till: str, adults: int,
                   children_ages: str | None, nights_min: int, nights_max: int,
                   budget_max: int | None, boards: str | None,
                   countries: str | None, only_hot: bool,
                   stars_min: int | None = None,
                   hotel_id: str | None = None,
                   limit: int = 100) -> tuple[list[str], dict]:
    """Shared WHERE + params for all offer-level queries. Party composition
    (adults + children_ages) must match exactly — price depends on it."""
    ages = _norm_ages(children_ages)
    where = ["o.date_start BETWEEN :date_from AND :date_till",
             "o.nights BETWEEN :nights_min AND :nights_max",
             "o.pax_adl = :adults",
             "o.children_ages = :ages"]
    params = {"date_from": date_from, "date_till": date_till,
              "nights_min": nights_min, "nights_max": nights_max,
              "adults": adults, "ages": ages, "limit": min(limit, 500)}
    if budget_max:
        where.append("l.price_cents <= :budget_cents")
        params["budget_cents"] = budget_max * 100
    if boards:
        codes = [b.strip() for b in boards.split(",") if b.strip()]
        if codes:
            marks = ",".join(f":b{i}" for i in range(len(codes)))
            params.update({f"b{i}": c for i, c in enumerate(codes)})
            where.append(f"o.board_code IN ({marks})")
    if countries:
        ids = [c.strip() for c in countries.split(",") if c.strip()]
        if ids:
            marks = ",".join(f":c{i}" for i in range(len(ids)))
            params.update({f"c{i}": c for i, c in enumerate(ids)})
            where.append(f"h.country_id IN ({marks})")
    if only_hot:
        where.append("l.is_hot = 1")
    if stars_min:
        # NULLIF guards Postgres against casting '' — NULL comparison drops the row
        where.append("CAST(NULLIF(h.category, '') AS INTEGER) >= :stars_min")
        params["stars_min"] = stars_min
    if hotel_id:
        where.append("o.source_hotel_id = :hotel_id")
        params["hotel_id"] = str(hotel_id)
    return where, params


def search_offers(conn, *, date_from: str, date_till: str,
                  adults: int = 2, children_ages: str | None = None,
                  nights_min: int = 1, nights_max: int = 30,
                  budget_max: int | None = None, boards: str | None = None,
                  countries: str | None = None, only_hot: bool = False,
                  stars_min: int | None = None, hotel_id: str | None = None,
                  sort: str = "price", limit: int = 100) -> list[dict]:
    """Offers whose latest snapshot matches the filters.

    budget_max is a price ceiling in whole currency units (also the price
    threshold for subscriptions).
    """
    where, params = _build_filters(
        date_from=date_from, date_till=date_till, adults=adults,
        children_ages=children_ages, nights_min=nights_min,
        nights_max=nights_max, budget_max=budget_max, boards=boards,
        countries=countries, only_hot=only_hot, stars_min=stars_min,
        hotel_id=hotel_id, limit=limit)
    sort_expr = SORTS.get(sort, SORTS["price"])

    sql = f"""
        WITH latest AS (
            SELECT ps.*, ROW_NUMBER() OVER (
                PARTITION BY offer_id ORDER BY fetched_at DESC) AS rn
            FROM price_snapshots ps
        ),
        best_review AS (
            SELECT r.*, ROW_NUMBER() OVER (
                PARTITION BY source, source_hotel_id
                ORDER BY (reviews_count IS NULL), reviews_count DESC) AS rrn
            FROM hotel_reviews r
            WHERE r.rating IS NOT NULL
        )
        SELECT o.id AS offer_id, o.source_hotel_id, o.date_start, o.date_end,
               o.nights, o.board_code, o.board_name, o.room_name, o.link,
               o.origin_name, o.pax_adl,
               h.name AS hotel_name, h.category, h.country_name, h.city_name,
               h.photo_url,
               l.price_cents, l.currency, l.is_hot, l.fetched_at,
               l.availability, l.operator_avg_price_cents,
               CAST(l.price_cents * 1.0 / o.nights AS INTEGER) AS price_per_night_cents,
               br.platform AS review_platform, br.rating AS review_rating,
               br.rating_scale AS review_scale, br.reviews_count AS review_count,
               br.url AS review_url,
               (SELECT count(*) FROM price_snapshots ps2
                WHERE ps2.offer_id = o.id) AS snapshots_count,
               (SELECT CAST(avg(price_cents) AS REAL) FROM price_snapshots ps5
                WHERE ps5.offer_id = o.id) AS avg_seen_cents,
               (SELECT min(price_cents) FROM price_snapshots ps3
                WHERE ps3.offer_id = o.id) AS min_seen_cents,
               (SELECT max(price_cents) FROM price_snapshots ps4
                WHERE ps4.offer_id = o.id) AS max_seen_cents
        FROM offers o
        JOIN hotels h ON h.source = o.source AND h.source_hotel_id = o.source_hotel_id
        JOIN latest l ON l.offer_id = o.id AND l.rn = 1
        LEFT JOIN best_review br ON br.source = o.source
             AND br.source_hotel_id = o.source_hotel_id AND br.rrn = 1
        WHERE {' AND '.join(where)}
        ORDER BY {sort_expr} ASC
        LIMIT :limit
    """
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def search_hotels_grouped(conn, *, sort: str = "price", **filters) -> list[dict]:
    """One row per hotel: its cheapest matching offer (by the chosen sort)
    plus variant stats. Same filters as search_offers."""
    where, params = _build_filters(**filters)
    sort_expr = SORTS.get(sort, SORTS["price"])
    sql = f"""
        WITH latest AS (
            SELECT ps.*, ROW_NUMBER() OVER (
                PARTITION BY offer_id ORDER BY fetched_at DESC) AS rn
            FROM price_snapshots ps
        ),
        best_review AS (
            SELECT r.*, ROW_NUMBER() OVER (
                PARTITION BY source, source_hotel_id
                ORDER BY (reviews_count IS NULL), reviews_count DESC) AS rrn
            FROM hotel_reviews r
            WHERE r.rating IS NOT NULL
        ),
        matched AS (
            SELECT o.id AS offer_id, o.source, o.source_hotel_id, o.date_start,
                   o.date_end, o.nights, o.board_code, o.board_name,
                   o.room_name, o.link, o.origin_name, o.pax_adl,
                   h.name AS hotel_name, h.category, h.country_name,
                   h.city_name, h.photo_url,
                   l.price_cents, l.currency, l.is_hot, l.fetched_at,
                   l.availability, l.operator_avg_price_cents,
                   CAST(l.price_cents * 1.0 / o.nights AS INTEGER) AS price_per_night_cents,
                   br.platform AS review_platform, br.rating AS review_rating,
                   br.rating_scale AS review_scale,
                   br.reviews_count AS review_count, br.url AS review_url,
                   {sort_expr} AS sort_key
            FROM offers o
            JOIN hotels h ON h.source = o.source AND h.source_hotel_id = o.source_hotel_id
            JOIN latest l ON l.offer_id = o.id AND l.rn = 1
            LEFT JOIN best_review br ON br.source = o.source
                 AND br.source_hotel_id = o.source_hotel_id AND br.rrn = 1
            WHERE {' AND '.join(where)}
        ),
        ranked AS (
            SELECT m.*,
                   ROW_NUMBER() OVER (PARTITION BY m.source, m.source_hotel_id
                                      ORDER BY m.sort_key ASC) AS hrn,
                   COUNT(*) OVER (PARTITION BY m.source, m.source_hotel_id) AS variants,
                   MIN(m.price_cents) OVER (PARTITION BY m.source, m.source_hotel_id) AS variants_min_cents,
                   MAX(m.price_cents) OVER (PARTITION BY m.source, m.source_hotel_id) AS variants_max_cents,
                   MIN(m.date_start) OVER (PARTITION BY m.source, m.source_hotel_id) AS variants_date_from,
                   MAX(m.date_start) OVER (PARTITION BY m.source, m.source_hotel_id) AS variants_date_till
            FROM matched m
        )
        SELECT * FROM ranked WHERE hrn = 1
        ORDER BY sort_key ASC
        LIMIT :limit
    """
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        r.pop("hrn", None)
        r.pop("sort_key", None)
    return rows


def price_drops(conn, *, adults: int = 2, children_ages: str | None = None,
                since: str | None = None, today: str | None = None,
                limit: int = 100) -> list[dict]:
    """Offers whose latest snapshot is cheaper than the previous one —
    biggest relative drop first. The product's reason to exist.

    since: only count drops observed after this ISO timestamp (staleness cut).
    today: exclude departures already gone.
    """
    params = {"adults": adults, "ages": _norm_ages(children_ages),
              "limit": min(limit, 300)}
    extra = ""
    if since:
        extra += " AND cur.fetched_at >= :since"
        params["since"] = since
    if today:
        extra += " AND o.date_start >= :today"
        params["today"] = today
    sql = f"""
        WITH ordered AS (
            SELECT ps.*, ROW_NUMBER() OVER (
                PARTITION BY offer_id ORDER BY fetched_at DESC) AS rn
            FROM price_snapshots ps
        )
        SELECT o.id AS offer_id, o.source_hotel_id, o.date_start, o.date_end,
               o.nights, o.board_code, o.board_name, o.room_name, o.link,
               o.pax_adl,
               h.name AS hotel_name, h.category, h.country_name, h.city_name,
               h.photo_url,
               cur.price_cents, cur.currency, cur.is_hot, cur.fetched_at,
               prev.price_cents AS prev_price_cents,
               prev.fetched_at AS prev_fetched_at,
               (prev.price_cents - cur.price_cents) AS drop_cents,
               (SELECT count(*) FROM price_snapshots ps2
                WHERE ps2.offer_id = o.id) AS snapshots_count
        FROM ordered cur
        JOIN ordered prev ON prev.offer_id = cur.offer_id AND prev.rn = 2
        JOIN offers o ON o.id = cur.offer_id
        JOIN hotels h ON h.source = o.source AND h.source_hotel_id = o.source_hotel_id
        WHERE cur.rn = 1 AND cur.price_cents < prev.price_cents
          AND o.pax_adl = :adults AND o.children_ages = :ages{extra}
        ORDER BY (prev.price_cents - cur.price_cents) * 1.0 / prev.price_cents DESC
        LIMIT :limit
    """
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
