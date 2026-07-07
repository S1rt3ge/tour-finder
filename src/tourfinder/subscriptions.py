"""Subscription evaluation: match saved searches against the latest
snapshots and record alerts.

An offer returned by the search already satisfies every filter, including
the price ceiling (filters.budget_max). We fire:
  - new_match:  first time this offer matches this subscription
  - price_drop: it matched before and is now cheaper than the last alert

Anything not cheaper than the last alert is silent, so a tab polling every
minute does not re-alert the same offer at the same price.
"""
import json
import sqlite3
from datetime import datetime, timezone

from .queries import search_offers

_ALLOWED = {"date_from", "date_till", "adults", "nights_min", "nights_max",
            "budget_max", "boards", "countries", "only_hot"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def evaluate(conn: sqlite3.Connection, sub: sqlite3.Row) -> int:
    """Evaluate one subscription, insert new alert rows, return their count."""
    filters = {k: v for k, v in json.loads(sub["filters"]).items() if k in _ALLOWED}
    if not filters.get("date_from") or not filters.get("date_till"):
        return 0

    matches = search_offers(conn, limit=500, **filters)
    now = _utcnow()
    created = 0
    for m in matches:
        offer_id = m["offer_id"]
        price = m["price_cents"]
        last = conn.execute(
            """SELECT price_cents FROM alerts
               WHERE subscription_id=? AND offer_id=?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (sub["id"], offer_id),
        ).fetchone()

        if last is None:
            reason = "new_match"
        elif price < last["price_cents"]:
            reason = "price_drop"
        else:
            continue

        conn.execute(
            """INSERT INTO alerts(subscription_id, offer_id, reason, price_cents,
                                  created_at, seen)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (sub["id"], offer_id, reason, price, now),
        )
        created += 1
    conn.commit()
    return created


def evaluate_all(conn: sqlite3.Connection) -> int:
    """Evaluate every enabled subscription. Returns total new alerts."""
    subs = conn.execute("SELECT * FROM subscriptions WHERE enabled=1").fetchall()
    return sum(evaluate(conn, s) for s in subs)
