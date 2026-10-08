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
    """Immutable administrators from server configuration, not approved users."""
    return {s for s in re.split(r"[,\s]+", os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""))
            if s.isdecimal() and int(s) > 0 and str(int(s)) == s}


def access_state(conn, user_id) -> str:
    if str(user_id) in allowed_user_ids():
        return "approved"
    row = conn.execute("SELECT status FROM telegram_access_requests WHERE user_id=:id",
                       {"id": str(user_id)}).fetchone()
    return row["status"] if row and row["status"] in {"pending", "approved", "denied"} else "not_requested"


def has_access(conn, user_id) -> bool:
    return access_state(conn, user_id) == "approved"


def approved_user_ids(conn) -> set[str]:
    return allowed_user_ids() | {str(row["user_id"]) for row in conn.execute(
        "SELECT user_id FROM telegram_access_requests WHERE status='approved'")}


def _request_access(conn, sender):
    conn.execute("""INSERT INTO telegram_access_requests(user_id,status,first_name,requested_at)
        VALUES (:id,'pending',:name,:now) ON CONFLICT(user_id) DO NOTHING""",
        {"id": str(sender["id"]), "name": str(sender.get("first_name", ""))[:128], "now": now_iso()})


def _decide_access(conn, target: str, decision: str, admin: str) -> bool:
    # Also serialize with a delivery claim for this recipient. A revocation
    # blocks future claims; a send already in flight cannot be recalled.
    if admin not in allowed_user_ids() or target in allowed_user_ids():
        return False
    conn.execute("UPDATE telegram_users SET updated_at=updated_at WHERE user_id=:id", {"id": target})
    changed = conn.execute("""UPDATE telegram_access_requests
        SET status=:status,decided_at=:now,decided_by=:admin
        WHERE user_id=:id AND status<>:status RETURNING user_id""",
        {"id": target, "status": decision, "now": now_iso(), "admin": admin}).fetchone()
    if decision == "denied":
        conn.execute("UPDATE telegram_users SET can_notify=0,updated_at=:now WHERE user_id=:id",
                     {"id": target, "now": now_iso()})
    return bool(changed)


def _access_list(conn, admin: int, status: str) -> dict:
    rows = conn.execute("""SELECT user_id,first_name FROM telegram_access_requests
        WHERE status=:status ORDER BY requested_at,user_id LIMIT 20""", {"status": status}).fetchall()
    title = "Заявки на доступ" if status == "pending" else "Одобренные пользователи"
    lines, keyboard = [title], []
    for row in rows:
        uid = str(row["user_id"])
        name = " ".join(str(row["first_name"]).split())[:64]
        lines.append(f"{name or 'Пользователь'} · {uid}")
        if status == "pending":
            keyboard.append([{"text": f"Одобрить {uid}", "callback_data": f"access:approve:{uid}"},
                             {"text": f"Отказать {uid}", "callback_data": f"access:deny:{uid}"}])
        else:
            keyboard.append([{"text": f"Отозвать {uid}", "callback_data": f"access:deny:{uid}"}])
    if not rows:
        lines.append("Список пуст.")
    else:
        lines.append("Показано до 20 записей. /requests — обновить заявки; /approved — одобренные. /deny ID — отозвать доступ.")
    response = {"method": "sendMessage", "chat_id": admin, "text": "\n".join(lines)}
    if keyboard:
        response["reply_markup"] = {"inline_keyboard": keyboard}
    return response


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
        conn.commit()
        return {"ok": True}
    owners = allowed_user_ids()
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        sender = callback.get("from") or {}
        message = callback.get("message") or {}
        if not isinstance(sender, dict) or not isinstance(message, dict):
            conn.commit()
            return {"ok": True}
        chat = message.get("chat") or {}
        uid = sender.get("id")
        match = re.fullmatch(r"access:(approve|deny):([1-9][0-9]{0,19})", str(callback.get("data", "")))
        if (not isinstance(chat, dict) or type(uid) is not int or str(uid) not in owners or sender.get("is_bot")
                or chat.get("type") != "private" or type(chat.get("id")) is not int
                or chat["id"] != uid or not match or not isinstance(callback.get("id"), str)):
            conn.commit()
            return {"ok": True}
        action, target = match.groups()
        changed = _decide_access(conn, target, "approved" if action == "approve" else "denied", str(uid))
        conn.commit()
        return {"method": "answerCallbackQuery", "callback_query_id": callback["id"],
                "text": ("Доступ одобрен. Пользователь может снова открыть приложение или нажать /start."
                         if action == "approve" else "Доступ отозван.") if changed else "Решение уже действует или заявка не найдена."}
    message = update.get("message") or {}
    if not isinstance(message, dict):
        conn.commit()
        return {"ok": True}
    sender, chat = message.get("from") or {}, message.get("chat") or {}
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        conn.commit()
        return {"ok": True}
    user_id = sender.get("id")
    if (type(user_id) is not int or user_id <= 0 or chat.get("type") != "private"
            or type(chat.get("id")) is not int or chat.get("id") != user_id or sender.get("is_bot")):
        conn.commit()
        return {"ok": True}
    words = str(message.get("text", "")).split(maxsplit=1)
    command = words[0].split("@")[0] if words else ""
    if str(user_id) in owners and command in {"/requests", "/approved"}:
        response = _access_list(conn, user_id, "pending" if command == "/requests" else "approved")
        conn.commit()
        return response
    if str(user_id) in owners and command in {"/deny", "/approve"}:
        target = words[1].strip() if len(words) > 1 else ""
        changed = bool(re.fullmatch(r"[1-9][0-9]{0,19}", target)) and _decide_access(
            conn, target, "denied" if command == "/deny" else "approved", str(user_id))
        conn.commit()
        return {"method": "sendMessage", "chat_id": user_id,
                "text": "Решение сохранено." if changed else "Решение не изменено. Используй /requests или /approved; владельцев отозвать нельзя."}
    if command == "/start" and str(user_id) not in owners:
        _request_access(conn, sender)
    approved = has_access(conn, user_id)
    if command in {"/start", "/stop"}:
        conn.execute(
            """INSERT INTO telegram_users(user_id,chat_id,first_name,can_notify,created_at,updated_at)
               VALUES (:uid,:uid,:name,:enabled,:now,:now)
               ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,
               first_name=excluded.first_name,can_notify=excluded.can_notify,updated_at=excluded.updated_at""",
            {"uid": str(user_id), "name": str(sender.get("first_name", ""))[:128],
             "enabled": int(command == "/start" and approved), "now": now_iso()})
    state = access_state(conn, user_id)
    conn.commit()
    if command == "/id":
        text = f"Твой Telegram ID: {user_id}"
    elif not approved:
        if command not in {"/start", "/app", "/help", "/stop"}:
            return {"ok": True}
        text = {"pending": "Заявка отправлена владельцу. Доступ появится после одобрения. Позже нажми /start или снова открой приложение.",
                "denied": "Владелец не одобрил доступ или отозвал его. Поиск и уведомления недоступны.",
                "not_requested": "Это закрытый бот. Нажми /start, чтобы запросить доступ у владельца."}[state]
    elif command == "/stop":
        text = "Уведомления остановлены. Сохранённые поиски остались в приложении. /start — включить снова."
    elif command in {"/start", "/app", "/help"}:
        text = "Tour Finder ищет туры из Риги. Открой приложение, выбери даты и состав туристов, затем сохрани поиск. Я пришлю подходящие предложения и объясню выгоду. /stop — остановить уведомления."
    else:
        return {"ok": True}
    response = {"method": "sendMessage", "chat_id": user_id, "text": text}
    if approved and command in {"/start", "/app", "/help"}:
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
        {"command": "start", "description": "Запросить доступ / включить уведомления"},
        {"command": "app", "description": "Открыть подбор туров"},
        {"command": "stop", "description": "Остановить уведомления"},
        {"command": "id", "description": "Мой Telegram ID"},
        {"command": "requests", "description": "Владельцу: заявки на доступ"},
        {"command": "approved", "description": "Владельцу: одобренные пользователи"},
        {"command": "deny", "description": "Владельцу: отозвать доступ по ID"}])
    client.call("setChatMenuButton", menu_button={"type": "web_app", "text": "Туры", "web_app": {"url": app_url}})
    parsed = urlsplit(app_url)
    client.call("setWebhook", url=f"{parsed.scheme}://{parsed.netloc}/api/telegram/webhook",
                secret_token=secret, allowed_updates=["message", "callback_query"], drop_pending_updates=False)
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
