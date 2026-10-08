"""Telegram trust boundary, webhook commands, and explicit BotFather setup.

The token stays server-side. Mini App requests are authenticated using raw
initData; user IDs supplied by the browser are never trusted.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

import requests


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def allowed_user_ids() -> set[str]:
    return {s for s in re.split(r"[,\s]+", os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""))
            if s.isdecimal() and int(s) > 0 and str(int(s)) == s}


def public_app_url() -> str:
    value = os.environ.get("TELEGRAM_APP_URL", "").strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("TELEGRAM_APP_URL must be an HTTPS URL")
    return value


class InitDataError(ValueError):
    pass


def validate_init_data(raw: str, token: str, *, now: int | None = None,
                       max_age: int = 3600) -> dict:
    if not token or not raw or len(raw) > 16384:
        raise InitDataError("Missing Telegram session")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("Duplicate fields")
        supplied = fields.pop("hash")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", supplied):
            raise ValueError("Invalid hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied.lower()):
            raise ValueError("Invalid signature")
        stamp = int(fields["auth_date"])
        current = int(time.time()) if now is None else now
        if current - stamp > max_age or stamp - current > 30:
            raise ValueError("Expired session; reopen the Mini App")
        user = json.loads(fields["user"])
        if (not isinstance(user, dict) or type(user.get("id")) is not int
                or user["id"] <= 0 or user.get("is_bot")):
            raise ValueError("Invalid user")
        return {"id": user["id"], "first_name": str(user.get("first_name", ""))[:128]}
    except (ValueError, TypeError, KeyError) as exc:
        raise InitDataError("Invalid or expired Telegram session") from exc


class BotAPIError(RuntimeError):
    def __init__(self, code: int = 0, *, retry_after: int | None = None,
                 ambiguous: bool = False):
        super().__init__(f"Telegram API error {code}" if code else "Telegram connection failed")
        self.code = code
        self.retry_after = retry_after
        self.ambiguous = ambiguous


class BotClient:
    def __init__(self, token: str | None = None):
        self.token = token or bot_token()
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured")

    def call(self, method: str, **payload) -> dict:
        if not re.fullmatch(r"[A-Za-z]+", method):
            raise ValueError("Invalid Telegram method")
        try:
            response = requests.post(f"https://api.telegram.org/bot{self.token}/{method}",
                                     json=payload, timeout=(5, 20))
            data = response.json()
        except (requests.RequestException, ValueError):
            # A send may have reached Telegram before the connection was lost.
            # Never expose requests exceptions: their URLs contain the token.
            raise BotAPIError(ambiguous=True) from None
        if not isinstance(data, dict) or not data.get("ok"):
            try:
                code = int(data.get("error_code") or response.status_code) if isinstance(data, dict) else response.status_code
            except (ValueError, TypeError):
                code = response.status_code
            params = (data.get("parameters") or {}) if isinstance(data, dict) else {}
            if not isinstance(params, dict):
                params = {}
            raise BotAPIError(code, retry_after=params.get("retry_after"), ambiguous=code >= 500)
        return data.get("result", {})


def webhook_response(conn, update: dict) -> dict:
    """Handle private commands once. Return a Telegram webhook API response.

    Returning sendMessage here avoids making a second network request while
    Telegram holds the webhook connection. Bot commands never evaluate tours.
    """
    update_id = update.get("update_id")
    if type(update_id) is not int:
        return {"ok": True}
    inserted = conn.execute(
        "INSERT INTO telegram_updates(update_id, processed_at) VALUES (:id,:now) "
        "ON CONFLICT(update_id) DO NOTHING RETURNING update_id",
        {"id": str(update_id), "now": now_iso()}).fetchone()
    if not inserted:
        return {"ok": True}
    message = update.get("message") or {}
    sender, chat = message.get("from") or {}, message.get("chat") or {}
    user_id = sender.get("id")
    if (type(user_id) is not int or chat.get("type") != "private"
            or chat.get("id") != user_id or sender.get("is_bot")):
        conn.commit()
        return {"ok": True}
    words = str(message.get("text", "")).split(maxsplit=1)
    command = words[0].split("@")[0] if words else ""
    owners = allowed_user_ids()
    if str(user_id) not in owners:
        conn.commit()
        # During setup, /id lets the owner discover their ID without a third-party bot.
        if not owners and command in {"/id", "/start"}:
            return {"method": "sendMessage", "chat_id": user_id,
                    "text": f"Твой Telegram ID: {user_id}. Добавь его в TELEGRAM_ALLOWED_USER_IDS на сервере, затем снова нажми /start."}
        return {"ok": True}
    if command in {"/start", "/stop"}:
        conn.execute(
            """INSERT INTO telegram_users(user_id,chat_id,first_name,can_notify,created_at,updated_at)
               VALUES (:uid,:uid,:name,:enabled,:now,:now)
               ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,
               first_name=excluded.first_name,can_notify=excluded.can_notify,updated_at=excluded.updated_at""",
            {"uid": str(user_id), "name": str(sender.get("first_name", ""))[:128],
             "enabled": int(command == "/start"), "now": now_iso()})
    conn.commit()
    if command == "/stop":
        text = "Уведомления остановлены. Сохранённые поиски остались в приложении. /start — включить снова."
    elif command == "/id":
        text = f"Твой Telegram ID: {user_id}"
    elif command in {"/start", "/app", "/help"}:
        text = "Tour Finder ищет туры из Риги. Открой приложение, выбери даты и состав туристов, затем сохрани поиск. Я пришлю подходящие предложения и объясню выгоду. /stop — остановить уведомления."
    else:
        return {"ok": True}
    response = {"method": "sendMessage", "chat_id": user_id, "text": text}
    if command in {"/start", "/app", "/help"}:
        try:
            response["reply_markup"] = {"inline_keyboard": [[{
                "text": "Открыть Tour Finder", "web_app": {"url": public_app_url()}}]]}
        except ValueError:
            pass
    return response


def configure() -> None:
    app_url = public_app_url()
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
        raise ValueError("TELEGRAM_WEBHOOK_SECRET must contain 32..256 URL-safe characters")
    client = BotClient()
    bot = client.call("getMe")
    client.call("setMyCommands", commands=[
        {"command": "start", "description": "Включить уведомления"},
        {"command": "app", "description": "Открыть подбор туров"},
        {"command": "stop", "description": "Остановить уведомления"},
        {"command": "id", "description": "Мой Telegram ID"}])
    client.call("setChatMenuButton", menu_button={"type": "web_app", "text": "Туры", "web_app": {"url": app_url}})
    parsed = urlsplit(app_url)
    client.call("setWebhook", url=f"{parsed.scheme}://{parsed.netloc}/api/telegram/webhook",
                secret_token=secret, allowed_updates=["message"], drop_pending_updates=False)
    print(f"Configured @{bot.get('username', '')}; open the bot and send /start.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["configure"])
    parser.add_argument("--env-file", help="Local ignored file containing server settings")
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file)
    try:
        configure()
    except (ValueError, BotAPIError) as error:
        parser.exit(1, f"{error}\n")
