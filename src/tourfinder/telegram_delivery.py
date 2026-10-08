"""Durable Telegram delivery with conservative handling of ambiguous sends.

The alert is claimed and committed before HTTP. Timed-out sends are never
blindly retried; their outcome stays uncertain and counts toward rate limits.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from . import db, subscriptions
from .telegram_bot import (BotAPIError, BotClient, allowed_user_ids, approved_user_ids,
                           bot_token, has_access, public_app_url)

MAX_PER_USER_24H = 3
MAX_ALERT_AGE = timedelta(hours=24)
MAX_OBSERVATION_AGE = timedelta(hours=6)
CLAIM_TIMEOUT = timedelta(minutes=10)


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time(value) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def _safe_url(value) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def _load_candidate(conn, alert_id):
    row = conn.execute("""
        SELECT d.alert_id, d.status, d.attempts, d.next_attempt_at,
               a.subscription_id, a.offer_id, a.price_cents AS alert_price_cents,
               a.created_at AS alert_created_at, a.evidence,
               s.owner_id, s.enabled, s.name AS subscription_name,
               u.chat_id, u.can_notify,
               o.source, o.source_hotel_id, o.date_start, o.nights,
               o.board_name, o.board_code, o.pax_adl, o.pax_chd, o.children_ages,
               o.link, o.last_seen_at,
               h.name AS hotel_name, h.country_name, h.city_name
        FROM telegram_deliveries d
        JOIN alerts a ON a.id=d.alert_id
        JOIN subscriptions s ON s.id=a.subscription_id
        LEFT JOIN telegram_users u ON u.user_id=s.owner_id
        JOIN offers o ON o.id=a.offer_id
        JOIN hotels h ON h.source=o.source AND h.source_hotel_id=o.source_hotel_id
        WHERE d.alert_id=:id
    """, {"id": alert_id}).fetchone()
    return dict(row) if row else None


def _ineligible(conn, row, owners, now):
    created = _time(row["alert_created_at"])
    if not created or created < now - MAX_ALERT_AGE or created > now + timedelta(minutes=1):
        return "alert_expired"
    if not row["enabled"]:
        return "subscription_disabled"
    if (not row["owner_id"] or str(row["owner_id"]) not in owners
            or not has_access(conn, row["owner_id"])):
        return "owner_not_allowed"
    if not row["can_notify"]:
        return "notifications_disabled"
    if str(row["chat_id"]) != str(row["owner_id"]):
        return "recipient_mismatch"
    if row["date_start"] < now.date().isoformat():
        return "departure_passed"
    seen = _time(row["last_seen_at"])
    if not seen or seen < now - MAX_OBSERVATION_AGE or seen > now + timedelta(minutes=1):
        return "offer_stale"
    latest = conn.execute("""
        SELECT price_cents,currency,fetched_at,stop_sale
        FROM price_snapshots WHERE offer_id=:id
        ORDER BY fetched_at DESC,id DESC LIMIT 1
    """, {"id": row["offer_id"]}).fetchone()
    if latest is None:
        return "price_missing"
    fetched = _time(latest["fetched_at"])
    if not fetched or fetched < now - MAX_OBSERVATION_AGE or fetched > now + timedelta(minutes=1):
        return "price_stale"
    if latest["price_cents"] != row["alert_price_cents"] or latest["price_cents"] <= 0:
        return "price_changed"
    if str(latest["currency"]).upper() != "EUR":
        return "unsupported_currency"
    if str(latest["stop_sale"] or "").strip().lower() not in {"", "0", "false", "no", "n"}:
        return "stop_sale"
    try:
        evidence = json.loads(row["evidence"] or "{}")
    except (ValueError, TypeError):
        return "invalid_evidence"
    if not isinstance(evidence, dict) or evidence.get("kind") not in {"budget", "deal"}:
        return "invalid_evidence"
    if evidence["kind"] == "deal":
        baseline, saving = evidence.get("baseline_cents"), evidence.get("saving_cents")
        pct = evidence.get("drop_pct")
        if (type(baseline) not in (int, float) or type(saving) not in (int, float)
                or type(pct) not in (int, float)
                or not all(math.isfinite(value) for value in (baseline, saving, pct))
                or baseline <= row["alert_price_cents"] or saving <= 0
                or abs(baseline - row["alert_price_cents"] - saving) > 1
                or abs(100 * saving / baseline - pct) > 0.2):
            return "invalid_evidence"
        observed = _time(evidence.get("drop_observed_at"))
        if (not observed or observed < now - timedelta(hours=72)
                or observed > now + timedelta(minutes=1)):
            return "deal_event_stale"
    row["parsed_evidence"] = evidence
    return None


def _quota(conn, row, now, reserved=()):
    # Denormalized recipient/hotel fields survive deletion of the subscription
    # and alert. Reserved and uncertain sends count conservatively as messages.
    recent = list(conn.execute("""
        SELECT subscription_id,source,source_hotel_id,
               COALESCE(sent_at,claimed_at) AS reserved_at
        FROM telegram_deliveries
        WHERE owner_id=:owner AND status IN ('sent','sending','uncertain')
          AND COALESCE(sent_at,claimed_at)>=:cutoff
        ORDER BY COALESCE(sent_at,claimed_at)
    """, {"owner": str(row["owner_id"]), "cutoff": _iso(now - MAX_ALERT_AGE)}).fetchall())
    recent.extend(r for r in reserved if r["owner_id"] == str(row["owner_id"]))
    recent.sort(key=lambda item: item["reserved_at"])
    if any(r["subscription_id"] == row["subscription_id"] and
           r["source"] == row["source"] and r["source_hotel_id"] == row["source_hotel_id"]
           for r in recent):
        return "hotel_daily_limit", None
    if len(recent) >= MAX_PER_USER_24H:
        oldest = _time(recent[0]["reserved_at"]) or now
        return "user_daily_limit", _iso(oldest + MAX_ALERT_AGE + timedelta(seconds=1))
    return None, None


def _discard(conn, alert_id, reason):
    conn.execute("""UPDATE telegram_deliveries SET status='discarded',last_error=:reason
                    WHERE alert_id=:id AND status IN ('pending','retry')""",
                 {"id": alert_id, "reason": reason})


def _claim(conn, alert_id, owners, now):
    """Serialize reservations per user, then atomically claim this one alert."""
    row = _load_candidate(conn, alert_id)
    if not row or row["status"] not in {"pending", "retry"}:
        conn.rollback()
        return None, "unavailable"
    if row["next_attempt_at"] and row["next_attempt_at"] > _iso(now):
        conn.rollback()
        return None, "not_due"
    owner = row["owner_id"]
    # Row lock lasts only through eligibility/quota checks and reservation;
    # there is no database transaction open while talking to Telegram.
    conn.execute("UPDATE telegram_users SET updated_at=updated_at WHERE user_id=:owner",
                 {"owner": owner})
    row = _load_candidate(conn, alert_id)
    if not row or row["owner_id"] != owner or row["status"] not in {"pending", "retry"}:
        conn.rollback()
        return None, "unavailable"
    if row["next_attempt_at"] and row["next_attempt_at"] > _iso(now):
        conn.rollback()
        return None, "not_due"
    reason = _ineligible(conn, row, owners, now)
    if reason:
        if reason == "notifications_disabled":
            # /start can arrive after subscription creation. Preserve this
            # pending alert until opt-in (or the normal 24-hour expiry).
            conn.execute("""UPDATE telegram_deliveries
                            SET status='retry',next_attempt_at=:next,last_error=:reason
                            WHERE alert_id=:id AND status IN ('pending','retry')""",
                         {"id": alert_id, "next": _iso(now + timedelta(minutes=15)),
                          "reason": reason})
        else:
            _discard(conn, alert_id, reason)
        conn.commit()
        return None, reason
    reason, next_attempt = _quota(conn, row, now)
    if reason:
        if next_attempt:
            conn.execute("""UPDATE telegram_deliveries
                            SET status='retry',next_attempt_at=:next,last_error=:reason
                            WHERE alert_id=:id AND status IN ('pending','retry')""",
                         {"id": alert_id, "next": next_attempt, "reason": reason})
        else:
            _discard(conn, alert_id, reason)
        conn.commit()
        return None, reason
    claimed = conn.execute("""
        UPDATE telegram_deliveries
        SET status='sending',attempts=attempts+1,claimed_at=:now,
            next_attempt_at=NULL,last_error=NULL,
            owner_id=:owner,subscription_id=:sub,source=:source,source_hotel_id=:hotel
        WHERE alert_id=:id AND status IN ('pending','retry')
          AND (next_attempt_at IS NULL OR next_attempt_at<=:now)
        RETURNING attempts
    """, {"id": alert_id, "now": _iso(now), "owner": str(owner),
           "sub": row["subscription_id"], "source": row["source"],
           "hotel": row["source_hotel_id"]}).fetchone()
    conn.commit()
    if not claimed:
        return None, "unavailable"
    row["attempts"] = claimed["attempts"]
    return row, None


def _plain(value, limit=160):
    return " ".join(str(value or "").split())[:limit]


def _message(row, app_url):
    evidence = row["parsed_evidence"]
    price = row["alert_price_cents"] / 100
    title = "Тур подешевел" if evidence["kind"] == "deal" else "Тур подходит по бюджету"
    place = ", ".join(_plain(row[key], 80) for key in ("country_name", "city_name") if row[key])
    party = f"{row['pax_adl']} взр."
    if row["pax_chd"]:
        party += f" + {row['pax_chd']} реб. ({_plain(row['children_ages'], 40)} лет)"
    lines = [title, _plain(row["hotel_name"]), place,
             f"{price:,.2f} € за весь состав · {row['nights']} ночей",
             f"Вылет {row['date_start']} из Риги · {party}",
             _plain(row["board_name"] or row["board_code"], 100)]
    if evidence["kind"] == "deal":
        lines.append(f"Ранее наблюдалось {evidence['baseline_cents'] / 100:,.2f} €; "
                     f"снижение {evidence['saving_cents'] / 100:,.2f} € ({evidence['drop_pct']:.1f}%).")
    else:
        lines.append("Цена вписывается в бюджет сохранённого поиска.")
    lines.append(f"Подписка: {_plain(row['subscription_name'], 100)}")
    lines.append("Цена и наличие проверяются у продавца при открытии.")
    buttons = [[{"text": "Открыть Tour Finder", "web_app": {"url": app_url}}]]
    source_url = _safe_url(row["link"])
    if source_url:
        buttons.append([{"text": "Проверить у продавца", "url": source_url}])
    return {"chat_id": row["chat_id"], "text": "\n".join(line for line in lines if line)[:3500],
            "reply_markup": {"inline_keyboard": buttons},
            "link_preview_options": {"is_disabled": True}}


def _finish(conn, row, status, now, *, reason=None, message_id=None, retry_at=None):
    conn.execute("""
        UPDATE telegram_deliveries SET status=:status,last_error=:reason,
            sent_at=:sent,message_id=:message,next_attempt_at=:retry
        WHERE alert_id=:id AND status='sending' AND attempts=:attempts
    """, {"id": row["alert_id"], "attempts": row["attempts"], "status": status,
           "reason": reason, "sent": _iso(now) if status == "sent" else None,
           "message": str(message_id) if message_id is not None else None, "retry": retry_at})
    conn.commit()


def run_worker(conn, *, client=None, dry_run=False, now=None, limit=100):
    owners = approved_user_ids(conn)
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    current = clock()
    summary = Counter()
    deadline = time.monotonic() + 6 * 60
    if not dry_run:
        if not owners:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS is empty")
        app_url = public_app_url()
        client = client or BotClient()
        # Alert evaluation is independent of the long-running collector.
        subscriptions.evaluate_all(conn, deadline=deadline)
        summary["uncertain_recovered"] = conn.execute("""
            UPDATE telegram_deliveries SET status='uncertain',last_error='previous_attempt_outcome_unknown'
            WHERE status='sending' AND (claimed_at IS NULL OR claimed_at<:cutoff)
        """, {"cutoff": _iso(current - CLAIM_TIMEOUT)}).rowcount
        summary["expired"] = conn.execute("""
            UPDATE telegram_deliveries SET status='discarded',last_error='alert_expired'
            WHERE status IN ('pending','retry')
              AND alert_id IN (SELECT id FROM alerts WHERE created_at<:cutoff)
        """, {"cutoff": _iso(current - MAX_ALERT_AGE)}).rowcount
        conn.commit()
    ids = [row["alert_id"] for row in conn.execute("""
        SELECT d.alert_id FROM telegram_deliveries d JOIN alerts a ON a.id=d.alert_id
        WHERE d.status IN ('pending','retry')
          AND (d.next_attempt_at IS NULL OR d.next_attempt_at<=:now)
        ORDER BY CASE WHEN a.reason='price_drop' THEN 0 ELSE 1 END,
                 a.price_cents,a.id LIMIT :limit
    """, {"now": _iso(current), "limit": limit}).fetchall()]
    if not dry_run:
        conn.commit()
    summary["candidates"] = len(ids)
    preview_reservations = []
    for alert_id in ids:
        if time.monotonic() >= deadline:
            summary["time_budget_reached"] += 1
            break
        current = clock()
        if dry_run:
            row = _load_candidate(conn, alert_id)
            reason = _ineligible(conn, row, owners, current) if row else "unavailable"
            if not reason:
                reason, _ = _quota(conn, row, current, preview_reservations)
            if not reason:
                preview_reservations.append({"owner_id": str(row["owner_id"]),
                    "subscription_id": row["subscription_id"], "source": row["source"],
                    "source_hotel_id": row["source_hotel_id"], "reserved_at": _iso(current)})
            summary[reason or "eligible"] += 1
            continue
        row, reason = _claim(conn, alert_id, owners, current)
        if not row:
            summary[reason] += 1
            continue
        try:
            result = client.call("sendMessage", **_message(row, app_url))
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if type(message_id) is not int or message_id <= 0:
                raise BotAPIError(ambiguous=True)
        except BotAPIError as exc:
            current = clock()
            if exc.ambiguous:
                _finish(conn, row, "uncertain", current, reason="send_outcome_unknown")
                summary["uncertain"] += 1
            elif exc.code == 429:
                try:
                    delay = max(1, min(86400, int(exc.retry_after or 60)))
                except (ValueError, TypeError):
                    delay = 60
                _finish(conn, row, "retry", current, reason="telegram_rate_limit",
                        retry_at=_iso(current + timedelta(seconds=delay)))
                summary["retry"] += 1
            else:
                if exc.code == 403:
                    conn.execute("UPDATE telegram_users SET can_notify=0,updated_at=:now WHERE user_id=:owner",
                                 {"owner": str(row["owner_id"]), "now": _iso(current)})
                _finish(conn, row, "failed", current, reason=f"telegram_http_{int(exc.code)}")
                summary["failed"] += 1
                if exc.code == 401:
                    break  # invalid token affects every subsequent recipient
        except Exception:
            # Unknown network/client failures may have sent the message.
            _finish(conn, row, "uncertain", clock(), reason="send_outcome_unknown")
            summary["uncertain"] += 1
        else:
            _finish(conn, row, "sent", clock(), message_id=message_id)
            summary["sent"] += 1
    return {"dry_run": dry_run, **dict(summary)}


def _readonly_connection(url):
    """No db.get_engine: even schema migration would violate --dry-run."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        if not parsed.database or parsed.database == ":memory:":
            raise ValueError("dry-run requires an existing database")
        path = Path(parsed.database).resolve()
        if not path.is_file():
            raise ValueError("dry-run database does not exist")
        engine = create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")
    else:
        engine = create_engine(parsed, poolclass=NullPool,
                               connect_args={"prepare_threshold": None})
    conn = db.DB(engine)
    conn.execute("PRAGMA query_only=ON" if conn.dialect == "sqlite" else "SET TRANSACTION READ ONLY")
    return conn, engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="read eligibility only; no writes or HTTP")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    raw_url = os.environ.get("DATABASE_URL", "")
    if not raw_url:
        raise ValueError("DATABASE_URL is required; the default local database is never used")
    if not args.dry_run and (not bot_token() or not allowed_user_ids()):
        raise ValueError("Telegram token and allowlist must be configured")
    url = raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = None
    if args.dry_run:
        conn, engine = _readonly_connection(url)
    else:
        conn = db.connect()
    try:
        summary = run_worker(conn, dry_run=args.dry_run, limit=args.limit)
        print(json.dumps(summary, sort_keys=True))
        return int(bool(summary.get("failed") or summary.get("uncertain") or summary.get("uncertain_recovered")))
    finally:
        conn.close()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # SQL/requests errors may include secrets; log only the exception type.
        print(f"Telegram delivery failed: {type(exc).__name__}")
        raise SystemExit(1)
