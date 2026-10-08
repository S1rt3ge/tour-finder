import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest
import requests

from tourfinder.telegram_bot import BotAPIError, BotClient, InitDataError, validate_init_data

TOKEN = "123:test-only-not-a-real-token"


def signed(user_id=123, stamp=1000, **extra):
    values = {"auth_date": str(stamp), "user": json.dumps({"id": user_id, "first_name": "Test"}), **extra}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, "\n".join(f"{k}={v}" for k, v in sorted(values.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_signed_data_including_signature():
    assert validate_init_data(signed(signature="additional-signed-field"), TOKEN, now=1000)["id"] == 123


@pytest.mark.parametrize("raw", [signed() + "&auth_date=1000", signed().replace("1000", "1001"),
                                 signed(stamp=1), signed(stamp=5000), signed(user_id=True),
                                 signed(user_id="123"), signed(user_id=-1), "hash=bad"])
def test_reject_untrusted_expired_or_malformed_sessions(raw):
    with pytest.raises(InitDataError):
        validate_init_data(raw, TOKEN, now=4000)


def test_wrong_token():
    with pytest.raises(InitDataError):
        validate_init_data(signed(), "other", now=1000)


def test_timeout_is_ambiguous_and_never_leaks_token(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.Timeout(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    monkeypatch.setattr(requests, "post", fail)
    with pytest.raises(BotAPIError) as captured:
        BotClient(TOKEN).call("sendMessage", chat_id=123, text="test")
    assert captured.value.ambiguous
    assert TOKEN not in str(captured.value)


def test_429_retains_retry_after(monkeypatch):
    class Response:
        status_code = 429
        def json(self):
            return {"ok": False, "error_code": 429, "parameters": {"retry_after": 12}}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response())
    with pytest.raises(BotAPIError) as captured:
        BotClient(TOKEN).call("sendMessage")
    assert captured.value.code == 429
    assert captured.value.retry_after == 12
    assert not captured.value.ambiguous


@pytest.fixture
def access_conn(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from tourfinder import db
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("TELEGRAM_APP_URL", "https://example.test/app")
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("no outbound HTTP in webhook tests"))
    engine = create_engine("sqlite:///" + (tmp_path / "access.sqlite").as_posix())
    db.metadata.create_all(engine)
    conn = db.DB(engine)
    yield conn
    conn.close()
    engine.dispose()


def command(update_id, user_id, text):
    return {"update_id": update_id, "message": {"from": {"id": user_id, "first_name": f"User {user_id}"},
            "chat": {"type": "private", "id": user_id}, "text": text}}


def decision(update_id, target=789, *, user=123, chat=123, chat_type="private", action="approve"):
    return {"update_id": update_id, "callback_query": {"id": str(update_id), "from": {"id": user},
            "message": {"chat": {"id": chat, "type": chat_type}}, "data": f"access:{action}:{target}"}}


def test_request_approval_stop_and_revoke_without_changing_admins(access_conn):
    from tourfinder import telegram_bot as bot
    conn = access_conn
    pending = bot.webhook_response(conn, command(1, 789, "/start"))
    assert pending["chat_id"] == 789 and "Заявка" in pending["text"]
    assert "reply_markup" not in pending
    assert bot.access_state(conn, 789) == "pending" and not bot.has_access(conn, 789)
    listing = bot.webhook_response(conn, command(2, 123, "/requests"))
    assert listing["chat_id"] == 123
    assert listing["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "access:approve:789"
    approved = bot.webhook_response(conn, decision(3))
    assert approved["method"] == "answerCallbackQuery"  # Acknowledge owner only.
    assert bot.has_access(conn, 789)
    assert bot.allowed_user_ids() == {"123"} and bot.approved_user_ids(conn) == {"123", "789"}
    # Approval does not start proactive messages. Requester checks using /start.
    assert conn.execute("SELECT can_notify FROM telegram_users WHERE user_id='789'").scalar() == 0
    bot.webhook_response(conn, command(4, 789, "/start"))
    assert conn.execute("SELECT can_notify FROM telegram_users WHERE user_id='789'").scalar() == 1
    bot.webhook_response(conn, command(5, 789, "/stop"))
    assert bot.has_access(conn, 789)
    assert conn.execute("SELECT can_notify FROM telegram_users WHERE user_id='789'").scalar() == 0
    approved_list = bot.webhook_response(conn, command(6, 123, "/approved"))
    assert approved_list["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "access:deny:789"
    bot.webhook_response(conn, decision(7, action="deny"))
    assert bot.access_state(conn, 789) == "denied" and not bot.has_access(conn, 789)
    bot.webhook_response(conn, command(8, 789, "/start"))
    assert bot.access_state(conn, 789) == "denied"  # /start cannot reset owner's decision.


@pytest.mark.parametrize("overrides", [
    {"user": 789, "chat": 789}, {"chat": 789}, {"chat_type": "group"},
    {"user": True, "chat": True}, {"user": 456, "chat": 456},
])
def test_callback_checks_sender_admin_and_matching_private_chat(access_conn, overrides):
    from tourfinder import telegram_bot as bot
    bot.webhook_response(access_conn, command(1, 789, "/start"))
    assert bot.webhook_response(access_conn, decision(2, **overrides)) == {"ok": True}
    assert bot.access_state(access_conn, 789) == "pending"


def test_callbacks_and_commands_are_idempotent_and_admins_cannot_be_revoked(access_conn):
    from tourfinder import telegram_bot as bot
    bot.webhook_response(access_conn, command(1, 789, "/start"))
    update = decision(2)
    bot.webhook_response(access_conn, update)
    before = dict(access_conn.execute("SELECT * FROM telegram_access_requests WHERE user_id='789'").fetchone())
    assert bot.webhook_response(access_conn, update) == {"ok": True}
    assert dict(access_conn.execute("SELECT * FROM telegram_access_requests WHERE user_id='789'").fetchone()) == before
    bot.webhook_response(access_conn, command(3, 789, "/deny 123"))
    bot.webhook_response(access_conn, command(4, 123, "/deny 123"))
    assert bot.has_access(access_conn, 123)
    bot.webhook_response(access_conn, command(5, 123, "/deny 789"))
    assert not bot.has_access(access_conn, 789)
    bot.webhook_response(access_conn, command(6, 123, "/approve 789"))
    assert bot.has_access(access_conn, 789)


def test_unapproved_user_cannot_read_other_requests_or_decide(access_conn):
    from tourfinder import telegram_bot as bot
    bot.webhook_response(access_conn, command(1, 999, "/start"))
    for number, text in enumerate(("/requests", "/approved", "/approve 999", "/deny 999"), 2):
        assert bot.webhook_response(access_conn, command(number, 789, text)) == {"ok": True}
    assert bot.access_state(access_conn, 999) == "pending"


def test_configure_registers_callback_updates_and_owner_commands(monkeypatch):
    from tourfinder import telegram_bot as bot
    calls = []
    monkeypatch.setenv("TELEGRAM_APP_URL", "https://example.test/app")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "x" * 32)
    fake = type("Client", (), {"call": lambda self, method, **data: calls.append((method, data)) or {"username": "fixture"}})()
    monkeypatch.setattr(bot, "BotClient", lambda: fake)
    bot.configure()
    commands = next(data["commands"] for method, data in calls if method == "setMyCommands")
    assert {item["command"] for item in commands} >= {"requests", "approved", "deny"}
    hook = next(data for method, data in calls if method == "setWebhook")
    assert set(hook["allowed_updates"]) == {"message", "callback_query"}
    assert hook["drop_pending_updates"] is False
