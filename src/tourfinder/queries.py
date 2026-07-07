"""Shared read queries over collected offers, used by the web UI and by
subscription evaluation."""
import sqlite3


def search_offers(conn: sqlite3.Connection, *, date_from: str, date_till: str,
                  adults: int = 2, nights_min: int = 1, nights_max: int = 30,
                  budget_max: int | None = None, boards: str | None = None,
                  countries: str | None = None, only_hot: bool = False,
                  limit: int = 100) -> list[dict]:
    """Offers whose latest snapshot matches the filters, cheapest first.

    budget_max is a price ceiling in whole currency units (also the price
    threshold for subscriptions).
    """
    where = ["o.date_start BETWEEN :date_from AND :date_till",
             "o.nights BETWEEN :nights_min AND :nights_max",
             "o.pax_adl = :adults"]
    params = {"date_from": date_from, "date_till": date_till,
              "nights_min": nights_min, "nights_max": nights_max,
              "adults": adults, "limit": min(limit, 500)}
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
        SELECT o.id AS offer_id, o.date_start, o.date_end, o.nights,
               o.board_code, o.board_name, o.room_name, o.link,
               o.origin_name, o.pax_adl,
               h.name AS hotel_name, h.category, h.country_name, h.city_name,
               h.photo_url,
               l.price_cents, l.currency, l.is_hot, l.fetched_at,
               l.availability, l.operator_avg_price_cents,
               br.platform AS review_platform, br.rating AS review_rating,
               br.rating_scale AS review_scale, br.reviews_count AS review_count,
               br.url AS review_url,
               (SELECT count(*) FROM price_snapshots ps2
                WHERE ps2.offer_id = o.id) AS snapshots_count,
               (SELECT avg(price_cents) FROM price_snapshots ps5
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
        ORDER BY l.price_cents ASC
        LIMIT :limit
    """
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
