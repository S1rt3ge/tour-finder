"""Evaluate personal saved searches and atomically enqueue explained alerts."""
import json
import time
from datetime import datetime, timedelta, timezone

from .deals import assess
from .queries import search_offers
from .telegram_bot import allowed_user_ids

_ALLOWED = {"date_from", "date_till", "adults", "children_ages", "nights_min",
            "nights_max", "budget_max", "boards", "countries", "only_hot", "stars_min"}


def evaluate(conn, sub) -> int:
    # Serialize collector and worker before reading previous alerts.
    locked = conn.execute("UPDATE subscriptions SET enabled=enabled WHERE id=:id AND enabled=1 RETURNING id",
                          {"id": sub["id"]}).fetchone()
    if not locked:
        conn.commit()
        return 0
    sub = dict(conn.execute("SELECT * FROM subscriptions WHERE id=:id", {"id": sub["id"]}).fetchone())
    if str(sub.get("owner_id")) not in allowed_user_ids():
        conn.commit()
        return 0
    filters = {k: v for k, v in json.loads(sub["filters"]).items() if k in _ALLOWED}
    if not filters.get("date_from") or not filters.get("date_till"):
        conn.commit()
        return 0
    now = datetime.now(timezone.utc)
    matches = search_offers(conn, limit=500, offset=sub["evaluation_offset"], **filters)
    histories = {}
    if matches and sub["notify_mode"] in {"deal", "both"}:
        params = {f"o{i}": m["offer_id"] for i, m in enumerate(matches)}
        marks = ",".join(f":{key}" for key in params)
        params["cutoff"] = (now - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for row in conn.execute(f"""SELECT id,offer_id,fetched_at,price_cents,currency FROM price_snapshots
            WHERE offer_id IN ({marks}) AND fetched_at>=:cutoff
            UNION ALL
            SELECT ps.id,ps.offer_id,ps.fetched_at,ps.price_cents,ps.currency
            FROM offers o JOIN price_snapshots ps ON ps.id=(
                SELECT anchor.id FROM price_snapshots anchor
                WHERE anchor.offer_id=o.id AND anchor.fetched_at<:cutoff
                ORDER BY anchor.fetched_at DESC,anchor.id DESC LIMIT 1)
            WHERE o.id IN ({marks})
            ORDER BY offer_id,fetched_at,id""", params):
            histories.setdefault(row["offer_id"], []).append(dict(row))
    policy = {**sub, "budget_max": filters.get("budget_max")}
    existing = {row["offer_id"]: row["price_cents"] for row in conn.execute(
        "SELECT offer_id,min(price_cents) AS price_cents FROM alerts WHERE subscription_id=:id GROUP BY offer_id",
        {"id": sub["id"]})}
    created = 0
    for match in matches:
        price = match["price_cents"]
        if price >= existing.get(match["offer_id"], float("inf")):
            continue
        evidence = assess(match, histories.get(match["offer_id"], []), policy, now=now)
        if not evidence:
            continue
        retained_id = conn.next_retained_id("alerts")
        id_column, id_value = ("id,", ":retained_id,") if retained_id is not None else ("", "")
        alert = conn.execute(
            f"""INSERT INTO alerts({id_column}subscription_id,offer_id,reason,price_cents,created_at,seen,evidence)
               VALUES ({id_value}:sub,:offer,:reason,:price,:now,0,:evidence) RETURNING id""",
            {"sub": sub["id"], "offer": match["offer_id"], "reason": "price_drop" if evidence["kind"] == "deal" else "new_match",
             "price": price, "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "evidence": json.dumps(evidence), "retained_id": retained_id}).fetchone()
        conn.execute("INSERT INTO telegram_deliveries(alert_id,status) VALUES (:id,'pending')", {"id": alert["id"]})
        created += 1
    conn.execute("UPDATE subscriptions SET evaluation_offset=:offset WHERE id=:id",
                 {"offset": sub["evaluation_offset"] + 500 if len(matches) == 500 else 0, "id": sub["id"]})
    conn.commit()
    return created


def evaluate_all(conn, *, deadline: float | None = None) -> int:
    rows = conn.execute("SELECT * FROM subscriptions WHERE enabled=1 AND owner_id IS NOT NULL ORDER BY id").fetchall()
    created = 0
    for sub in rows:
        if deadline is not None and time.monotonic() >= deadline:
            break
        created += evaluate(conn, sub)
    return created
