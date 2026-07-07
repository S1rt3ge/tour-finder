"""Local web UI: search over collected offers, no direction required."""
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from . import db, subscriptions
from .queries import search_offers

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"

app = FastAPI(title="tour-finder")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_conn():
    return db.connect()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    conn = get_conn()
    try:
        rows = search_offers(
            conn, date_from=date_from, date_till=date_till, adults=adults,
            nights_min=nights_min, nights_max=nights_max, budget_max=budget_max,
            boards=boards, countries=countries, only_hot=only_hot, limit=limit)
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


# --- subscriptions (saved searches / watchlist) ---------------------------

def _sub_dict(conn, row) -> dict:
    unseen = conn.execute(
        "SELECT count(*) FROM alerts WHERE subscription_id=? AND seen=0",
        (row["id"],),
    ).fetchone()[0]
    return {"id": row["id"], "name": row["name"],
            "filters": json.loads(row["filters"]), "enabled": bool(row["enabled"]),
            "created_at": row["created_at"], "unseen": unseen}


@app.get("/api/subscriptions")
def list_subscriptions():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM subscriptions ORDER BY id DESC").fetchall()
        return JSONResponse({"subscriptions": [_sub_dict(conn, r) for r in rows]})
    finally:
        conn.close()


@app.post("/api/subscriptions")
def create_subscription(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip() or "Без названия"
    filters = payload.get("filters") or {}
    if not filters.get("date_from") or not filters.get("date_till"):
        return JSONResponse({"error": "filters need date_from and date_till"},
                            status_code=400)
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO subscriptions(name, filters, enabled, created_at) "
            "VALUES (?, ?, 1, ?)",
            (name, json.dumps(filters), _utcnow()),
        )
        sub_id = cur.lastrowid
        conn.commit()
        # Evaluate immediately so the new subscription surfaces current matches.
        sub = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        new_alerts = subscriptions.evaluate(conn, sub)
        return JSONResponse({"id": sub_id, "new_alerts": new_alerts})
    finally:
        conn.close()


@app.patch("/api/subscriptions/{sub_id}")
def update_subscription(sub_id: int, payload: dict = Body(...)):
    conn = get_conn()
    try:
        if "enabled" in payload:
            conn.execute("UPDATE subscriptions SET enabled=? WHERE id=?",
                         (1 if payload["enabled"] else 0, sub_id))
        if "name" in payload:
            conn.execute("UPDATE subscriptions SET name=? WHERE id=?",
                         (str(payload["name"]).strip() or "Без названия", sub_id))
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@app.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


# --- alerts ----------------------------------------------------------------

@app.post("/api/poll")
def poll():
    """Evaluate enabled subscriptions and return unseen alerts with offer
    detail. The browser tab calls this on an interval and beeps on anything
    new. Only touches the local DB — no calls to the source."""
    conn = get_conn()
    try:
        subscriptions.evaluate_all(conn)
        rows = conn.execute(
            """SELECT a.id, a.subscription_id, a.offer_id, a.reason,
                      a.price_cents, a.created_at, a.seen,
                      s.name AS sub_name,
                      h.name AS hotel_name, h.category, h.country_name,
                      h.city_name, o.date_start, o.nights, o.board_code,
                      o.board_name, o.link
               FROM alerts a
               JOIN subscriptions s ON s.id = a.subscription_id
               JOIN offers o ON o.id = a.offer_id
               JOIN hotels h ON h.source=o.source AND h.source_hotel_id=o.source_hotel_id
               WHERE a.seen = 0
               ORDER BY a.created_at DESC, a.id DESC
               LIMIT 200""").fetchall()
        return JSONResponse({"unseen": len(rows),
                             "alerts": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.post("/api/alerts/seen")
def mark_alerts_seen(payload: dict = Body(default={})):
    ids = payload.get("ids")
    conn = get_conn()
    try:
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute(f"UPDATE alerts SET seen=1 WHERE id IN ({marks})", ids)
        else:
            conn.execute("UPDATE alerts SET seen=1 WHERE seen=0")
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()
