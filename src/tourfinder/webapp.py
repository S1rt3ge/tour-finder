"""Local web UI: search over collected offers, no direction required."""
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from . import db

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"

app = FastAPI(title="tour-finder")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_conn():
    return db.connect()


@app.get("/")
def index(request: Request):
    conn = get_conn()
    try:
        countries = conn.execute(
            """SELECT DISTINCT country_id, country_name FROM hotels
               WHERE country_id IS NOT NULL ORDER BY country_name"""
        ).fetchall()
        boards = conn.execute(
            """SELECT board_code, max(board_name) AS board_name FROM offers
               GROUP BY board_code ORDER BY board_code"""
        ).fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "index.html", {
        "countries": [dict(c) for c in countries],
        "boards": [dict(b) for b in boards],
    })


@app.get("/api/search")
def search(
    date_from: str = Query(...),
    date_till: str = Query(...),
    adults: int = 2,
    nights_min: int = 1,
    nights_max: int = 30,
    budget_max: int | None = None,
    boards: str | None = None,
    countries: str | None = None,
    only_hot: bool = False,
    limit: int = 100,
):
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
        marks = ",".join(f":b{i}" for i in range(len(codes)))
        params.update({f"b{i}": c for i, c in enumerate(codes)})
        where.append(f"o.board_code IN ({marks})")
    if countries:
        ids = [c.strip() for c in countries.split(",") if c.strip()]
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
        )
        SELECT o.id AS offer_id, o.date_start, o.date_end, o.nights,
               o.board_code, o.board_name, o.room_name, o.link,
               o.origin_name, o.pax_adl,
               h.name AS hotel_name, h.category, h.country_name, h.city_name,
               h.photo_url,
               l.price_cents, l.currency, l.is_hot, l.fetched_at,
               l.operator_avg_price_cents,
               (SELECT count(*) FROM price_snapshots ps2
                WHERE ps2.offer_id = o.id) AS snapshots_count,
               (SELECT min(price_cents) FROM price_snapshots ps3
                WHERE ps3.offer_id = o.id) AS min_seen_cents,
               (SELECT max(price_cents) FROM price_snapshots ps4
                WHERE ps4.offer_id = o.id) AS max_seen_cents
        FROM offers o
        JOIN hotels h ON h.source = o.source AND h.source_hotel_id = o.source_hotel_id
        JOIN latest l ON l.offer_id = o.id AND l.rn = 1
        WHERE {' AND '.join(where)}
        ORDER BY l.price_cents ASC
        LIMIT :limit
    """
    conn = get_conn()
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    return JSONResponse({"count": len(rows), "results": rows})


@app.get("/api/offers/{offer_id}/history")
def offer_history(offer_id: int):
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT fetched_at, price_cents, currency, is_hot, availability
               FROM price_snapshots WHERE offer_id=? ORDER BY fetched_at""",
            (offer_id,),
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({"offer_id": offer_id,
                         "history": [dict(r) for r in rows]})
