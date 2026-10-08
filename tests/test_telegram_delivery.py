"""Durable delivery tests use explicit temporary SQLite and a fake Bot API."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from tourfinder import db, subscriptions, telegram_delivery as delivery
from tourfinder.telegram_bot import BotAPIError

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,456")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-never-used-for-http")
    monkeypatch.setenv("TELEGRAM_APP_URL", "https://example.invalid/app")
    monkeypatch.setattr(subscriptions, "evaluate_all", Mock(return_value=0))
    monkeypatch.setattr("requests.post", Mock(side_effect=AssertionError("external HTTP forbidden")))
    engine = create_engine(f"sqlite:///{(tmp_path / 'delivery.sqlite').as_posix()}")
    db.metadata.create_all(engine)
    connection = db.DB(engine)
    yield connection
    connection.close()
    engine.dispose()


def pending(conn, number=1, *, owner="123", hotel=None, sub_id=None, can_notify=1,
            enabled=1, at=NOW, alert_age=timedelta(), observation_age=timedelta(),
            price=100000, currency="EUR", stop_sale=None, evidence=None, link=None):
    sub_id = sub_id or int(owner)
    hotel_id = "teztour:" + str(hotel or number)
    stamp = delivery._iso(at)
    seen = delivery._iso(at - observation_age)
    conn.execute("""INSERT INTO telegram_users(user_id,chat_id,can_notify,created_at,updated_at)
                    VALUES (:u,:u,:enabled,:now,:now) ON CONFLICT(user_id) DO NOTHING""",
                 {"u": owner, "enabled": can_notify, "now": stamp})
    conn.execute("""INSERT INTO subscriptions(id,name,filters,owner_id,enabled,created_at)
                    VALUES (:id,'Fixture search','{}',:owner,:enabled,:now)
                    ON CONFLICT(id) DO NOTHING""",
                 {"id": sub_id, "owner": owner, "enabled": enabled, "now": stamp})
    conn.execute("""INSERT INTO hotels(source,source_hotel_id,name,country_name,city_name)
                    VALUES ('waavo',:hotel,'<b>Fixture hotel</b>','Country','City')
                    ON CONFLICT(source,source_hotel_id) DO NOTHING""", {"hotel": hotel_id})
    conn.execute("""INSERT INTO offers(id,source,source_hotel_id,origin_id,date_start,nights,
                    board_code,room_code,pax_adl,pax_chd,first_seen_at,last_seen_at,link)
                    VALUES (:id,'waavo',:hotel,'RIX','2026-10-20',7,'AI',:room,2,0,:seen,:seen,:link)""",
                 {"id": number, "hotel": hotel_id, "room": str(number), "seen": seen,
                  "link": link or "https://example.invalid/tour"})
    conn.execute("""INSERT INTO price_snapshots(offer_id,fetched_at,price_cents,currency,stop_sale)
                    VALUES (:id,:seen,:price,:currency,:stop)""",
                 {"id": number, "seen": seen, "price": price, "currency": currency, "stop": stop_sale})
    conn.execute("""INSERT INTO alerts(id,subscription_id,offer_id,reason,price_cents,created_at,evidence)
                    VALUES (:id,:sub,:id,'new_match',:price,:created,:evidence)""",
                 {"id": number, "sub": sub_id, "price": price,
                  "created": delivery._iso(at - alert_age),
                  "evidence": json.dumps(evidence if evidence is not None else {"kind": "budget"})})
    conn.execute("INSERT INTO telegram_deliveries(alert_id) VALUES (:id)", {"id": number})
    conn.commit()
    return number


def client(*, effect=None):
    return type("FakeBot", (), {"call": Mock(side_effect=effect, return_value={"message_id": 42})})()


def state(conn, number=1):
    return dict(conn.execute("SELECT * FROM telegram_deliveries WHERE alert_id=:id",
                             {"id": number}).fetchone())


def test_claim_is_visible_before_http_and_sent_only_once(conn):
    pending(conn)

    def observed_send(method, **payload):
        other = db.DB(conn.engine)
        try:
            assert state(other)["status"] == "sending"
            assert state(other)["attempts"] == 1
        finally:
            other.close()
        assert method == "sendMessage"
        assert "parse_mode" not in payload
        assert payload["chat_id"] == "123"
        assert "<b>Fixture hotel</b>" in payload["text"]  # displayed literally
        return {"message_id": 43}

    bot = client(effect=observed_send)
    result = delivery.run_worker(conn, client=bot, now=NOW)
    assert result["sent"] == 1
    assert state(conn)["status"] == "sent"
    assert state(conn)["message_id"] == "43"
    delivery.run_worker(conn, client=bot, now=NOW)
    assert bot.call.call_count == 1


def test_user_limit_is_three_across_subscriptions(conn):
    for number in range(1, 5):
        pending(conn, number, sub_id=100 + number)
    bot = client()
    result = delivery.run_worker(conn, client=bot, now=NOW)
    assert bot.call.call_count == result["sent"] == 3
    assert result["user_daily_limit"] == 1
    assert state(conn, 4)["status"] == "retry"
    assert state(conn, 4)["next_attempt_at"] > delivery._iso(NOW + timedelta(hours=24))


def test_user_quota_survives_deleted_subscriptions_and_alerts(conn):
    for number in range(1, 4):
        pending(conn, number)
    delivery.run_worker(conn, client=client(), now=NOW)
    conn.execute("DELETE FROM alerts")
    conn.execute("DELETE FROM subscriptions")
    conn.commit()
    pending(conn, 4, sub_id=999)
    bot = client()
    result = delivery.run_worker(conn, client=bot, now=NOW)
    assert result["user_daily_limit"] == 1
    bot.call.assert_not_called()


def test_hotels_are_deduplicated_within_subscription(conn):
    pending(conn, 1, hotel=7)
    pending(conn, 2, hotel=7)
    result = delivery.run_worker(conn, client=client(), now=NOW)
    assert result["sent"] == 1 and result["hotel_daily_limit"] == 1
    assert state(conn, 2)["status"] == "discarded"


def test_inflight_reservations_count_and_second_claim_cannot_send_twice(conn):
    for number in range(1, 5):
        pending(conn, number)
    for number in range(1, 4):
        row, reason = delivery._claim(conn, number, {"123"}, NOW)
        assert row and reason is None
    second = db.DB(conn.engine)
    try:
        assert delivery._claim(second, 1, {"123"}, NOW) == (None, "unavailable")
        assert delivery._claim(second, 4, {"123"}, NOW) == (None, "user_daily_limit")
    finally:
        second.close()


def test_rolling_quota_expires_after_twenty_four_hours(conn):
    for number in range(1, 4):
        pending(conn, number)
    delivery.run_worker(conn, client=client(), now=NOW)
    later = NOW + timedelta(hours=25)
    pending(conn, 4, at=later)
    bot = client()
    assert delivery.run_worker(conn, client=bot, now=later)["sent"] == 1


@pytest.mark.parametrize("kwargs,reason", [
    ({"can_notify": 0}, "notifications_disabled"),
    ({"enabled": 0}, "subscription_disabled"),
    ({"owner": "789"}, "owner_not_allowed"),
    ({"currency": "USD"}, "unsupported_currency"),
    ({"stop_sale": "1"}, "stop_sale"),
    ({"observation_age": timedelta(hours=7)}, "offer_stale"),
    ({"evidence": {"kind": "deal", "baseline_cents": float("nan"), "saving_cents": 20000, "drop_pct": 20}}, "invalid_evidence"),
])
def test_ineligible_offers_never_send(conn, kwargs, reason):
    pending(conn, **kwargs)
    bot = client()
    result = delivery.run_worker(conn, client=bot, now=NOW)
    assert result[reason] == 1
    assert state(conn)["status"] == ("retry" if reason == "notifications_disabled" else "discarded")
    bot.call.assert_not_called()


def test_expired_alert_is_discarded_without_http(conn):
    pending(conn, alert_age=timedelta(hours=24, minutes=1))
    bot = client()
    result = delivery.run_worker(conn, client=bot, now=NOW)
    assert result["expired"] == 1
    assert state(conn)["last_error"] == "alert_expired"
    bot.call.assert_not_called()


def test_price_changed_at_same_timestamp_uses_snapshot_id_tiebreak(conn):
    pending(conn)
    conn.execute("""INSERT INTO price_snapshots(offer_id,fetched_at,price_cents,currency)
                    VALUES (1,:now,99000,'EUR')""", {"now": delivery._iso(NOW)})
    conn.commit()
    bot = client()
    assert delivery.run_worker(conn, client=bot, now=NOW)["price_changed"] == 1
    bot.call.assert_not_called()


def test_retry_after_is_respected_and_success_completes_second_attempt(conn):
    pending(conn)
    bot = client(effect=[BotAPIError(429, retry_after=120), {"message_id": 10}])
    assert delivery.run_worker(conn, client=bot, now=NOW)["retry"] == 1
    assert state(conn)["next_attempt_at"] == delivery._iso(NOW + timedelta(seconds=120))
    delivery.run_worker(conn, client=bot, now=NOW + timedelta(seconds=1))
    assert bot.call.call_count == 1
    assert delivery.run_worker(conn, client=bot, now=NOW + timedelta(seconds=121))["sent"] == 1
    assert state(conn)["attempts"] == 2


def test_forbidden_disables_notifications_and_suppresses_remaining_queue(conn):
    pending(conn, 1)
    pending(conn, 2)
    bot = client(effect=BotAPIError(403))
    result = delivery.run_worker(conn, client=bot, now=NOW)
    assert result["failed"] == 1 and result["notifications_disabled"] == 1
    assert conn.execute("SELECT can_notify FROM telegram_users WHERE user_id='123'").scalar() == 0
    assert bot.call.call_count == 1


def test_timeout_is_uncertain_and_never_automatically_retried(conn):
    pending(conn)
    bot = client(effect=BotAPIError(ambiguous=True))
    assert delivery.run_worker(conn, client=bot, now=NOW)["uncertain"] == 1
    assert state(conn)["status"] == "uncertain"
    delivery.run_worker(conn, client=bot, now=NOW + timedelta(minutes=20))
    assert bot.call.call_count == 1


def test_old_sending_claim_becomes_uncertain_without_resending(conn):
    pending(conn)
    delivery._claim(conn, 1, {"123"}, NOW)
    bot = client()
    result = delivery.run_worker(conn, client=bot, now=NOW + timedelta(minutes=11))
    assert result["uncertain_recovered"] == 1
    assert state(conn)["status"] == "uncertain"
    bot.call.assert_not_called()


def test_valid_deal_explains_observed_saving_and_rejects_unsafe_source_url(conn):
    pending(conn, link="javascript:alert(1)", evidence={"kind": "deal",
        "baseline_cents": 125000, "saving_cents": 25000, "drop_pct": 20,
        "drop_observed_at": delivery._iso(NOW)})
    bot = client()
    assert delivery.run_worker(conn, client=bot, now=NOW)["sent"] == 1
    payload = bot.call.call_args.kwargs
    assert "20.0%" in payload["text"] and "250.00" in payload["text"]
    assert len(payload["reply_markup"]["inline_keyboard"]) == 1
    assert payload["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == "https://example.invalid/app"


def test_pending_opt_in_is_retained_and_delivered_after_start(conn):
    pending(conn, can_notify=0)
    bot = client()
    first = delivery.run_worker(conn, client=bot, now=NOW)
    assert first["notifications_disabled"] == 1
    assert state(conn)["status"] == "retry"
    assert state(conn)["attempts"] == 0
    conn.execute("UPDATE telegram_users SET can_notify=1 WHERE user_id='123'")
    conn.commit()
    second = delivery.run_worker(conn, client=bot, now=NOW + timedelta(minutes=16))
    assert second["sent"] == 1
    assert bot.call.call_count == 1


def test_deal_event_older_than_seventy_two_hours_is_rechecked_at_send(conn):
    pending(conn, evidence={"kind": "deal", "baseline_cents": 125000,
        "saving_cents": 25000, "drop_pct": 20,
        "drop_observed_at": delivery._iso(NOW - timedelta(hours=72, minutes=1))})
    bot = client()
    assert delivery.run_worker(conn, client=bot, now=NOW)["deal_event_stale"] == 1
    assert state(conn)["status"] == "discarded"
    bot.call.assert_not_called()


def test_price_drops_have_priority_over_budget_alerts(conn):
    for number in range(1, 5):
        pending(conn, number)
    conn.execute("UPDATE alerts SET reason='price_drop' WHERE id=4")
    conn.commit()
    bot = client()
    delivery.run_worker(conn, client=bot, now=NOW)
    assert state(conn, 4)["status"] == "sent"
    assert state(conn, 3)["status"] == "retry"
    assert bot.call.call_count == 3


def test_source_no_stop_sale_marker_n_is_supported(conn):
    pending(conn, stop_sale="n")
    assert delivery.run_worker(conn, client=client(), now=NOW)["sent"] == 1


def test_dry_run_has_no_http_no_writes_and_simulates_quota(conn):
    for number in range(1, 5):
        pending(conn, number)
    before = [state(conn, number) for number in range(1, 5)]
    bot = client()
    result = delivery.run_worker(conn, client=bot, dry_run=True, now=NOW)
    assert result["eligible"] == 3 and result["user_daily_limit"] == 1
    assert [state(conn, number) for number in range(1, 5)] == before
    bot.call.assert_not_called()
    subscriptions.evaluate_all.assert_not_called()


def test_dry_run_connection_does_not_create_or_migrate_schema(conn, tmp_path, monkeypatch):
    import hashlib
    path = conn.engine.url.database
    before = hashlib.sha256(open(path, "rb").read()).hexdigest()
    monkeypatch.setattr(db, "get_engine", Mock(side_effect=AssertionError("schema initialization forbidden")))
    readonly, engine = delivery._readonly_connection(str(conn.engine.url))
    try:
        assert readonly.execute("SELECT count(*) FROM offers").scalar() == 0
    finally:
        readonly.close()
        engine.dispose()
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == before
    missing = tmp_path / "must-not-be-created.sqlite"
    with pytest.raises(ValueError, match="does not exist"):
        delivery._readonly_connection(f"sqlite:///{missing.as_posix()}")
    assert not missing.exists()
