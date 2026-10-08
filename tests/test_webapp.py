import hashlib
import hmac
import json
import time
from datetime import date, timedelta
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from tourfinder import db
from tourfinder.webapp import app

TOKEN = "123:fixture-only"


def auth(user=123):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user, "first_name": "Test"})}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + (tmp_path / "api.sqlite").as_posix())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,456")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "fixture-webhook-secret")
    monkeypatch.setenv("TELEGRAM_APP_URL", "https://example.test/app")
    with TestClient(app) as api:
        yield api
    for engine in db._engines.values():
        engine.dispose()
    db._engines.clear()


def payload():
    start = date.today() + timedelta(days=2)
    return {"name": "Отпуск", "filters": {"date_from": start.isoformat(), "date_till": (start + timedelta(days=10)).isoformat()}}


def test_preview_and_readonly_search_without_bot(client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert client.get("/app").status_code == 200
    assert client.get("/api/search", params=payload()["filters"]).status_code == 200
    assert client.get("/api/subscriptions").status_code == 503
    conn = db.connect()
    assert conn.execute("SELECT count(*) FROM pax_requests").scalar() == 0
    conn.close()


def test_owner_scope_and_mutation_auth(client):
    assert client.post("/api/subscriptions", json=payload()).status_code == 401
    assert client.get("/api/subscriptions", headers=auth(789)).status_code == 403
    created = client.post("/api/subscriptions", json=payload(), headers=auth())
    assert created.status_code == 200, created.text
    sub_id = created.json()["id"]
    assert created.json()["evaluation_pending"] is True
    assert len(client.get("/api/subscriptions", headers=auth()).json()["subscriptions"]) == 1
    assert client.get("/api/subscriptions", headers=auth(456)).json()["subscriptions"] == []
    assert client.patch(f"/api/subscriptions/{sub_id}", json={"enabled": False}, headers=auth(456)).status_code == 404
    assert client.delete(f"/api/subscriptions/{sub_id}", headers=auth(456)).status_code == 404
    assert client.patch(f"/api/subscriptions/{sub_id}", json={"enabled": False}, headers=auth()).status_code == 200
    assert client.get("/api/subscriptions", headers=auth()).json()["subscriptions"][0]["enabled"] is False


def test_invalid_filters_and_policies(client):
    item = payload()
    item["filters"]["children_ages"] = "-1,99"
    assert client.post("/api/subscriptions", json=item, headers=auth()).status_code == 422
    item = {**payload(), "notify_mode": "both"}
    assert client.post("/api/subscriptions", json=item, headers=auth()).status_code == 422
    item["filters"]["budget_max"] = 1500
    assert client.post("/api/subscriptions", json=item, headers=auth()).status_code == 200
    assert client.get("/api/search", params={**payload()["filters"], "adults": 0}).status_code == 400
    assert client.get("/api/drops?children_ages=no").status_code == 400


def test_seen_only_explicit_ids_and_owner(client):
    first = client.post("/api/subscriptions", json=payload(), headers=auth()).json()["id"]
    second = client.post("/api/subscriptions", json=payload(), headers=auth(456)).json()["id"]
    conn = db.connect()
    for number, sub_id in enumerate((first, second), 1):
        conn.execute("INSERT INTO alerts(id,subscription_id,offer_id,reason,price_cents,created_at) VALUES (:id,:sub,1,'new_match',100,'2026-10-08T00:00:00Z')", {"id": number, "sub": sub_id})
    conn.commit()
    assert client.post("/api/alerts/seen", json={}, headers=auth()).status_code == 400
    assert client.post("/api/alerts/seen", json={"ids": [1, 2]}, headers=auth()).status_code == 200
    assert [r["seen"] for r in conn.execute("SELECT seen FROM alerts ORDER BY id")] == [1, 0]
    conn.close()


def test_webhook_secret_start_stop_and_duplicate(client):
    update = {"update_id": 1, "message": {"from": {"id": 123, "first_name": "Test"}, "chat": {"id": 123, "type": "private"}, "text": "/start"}}
    assert client.post("/api/telegram/webhook", json=update).status_code == 403
    headers = {"X-Telegram-Bot-Api-Secret-Token": "fixture-webhook-secret"}
    response = client.post("/api/telegram/webhook", json=update, headers=headers)
    assert response.json()["method"] == "sendMessage"
    assert client.get("/api/telegram/session", headers=auth()).json()["can_notify"] is True
    assert client.post("/api/telegram/webhook", json=update, headers=headers).json() == {"ok": True}
    update["update_id"] = 2
    update["message"]["text"] = "/stop"
    assert client.post("/api/telegram/webhook", json=update, headers=headers).status_code == 200
    assert client.get("/api/telegram/session", headers=auth()).json()["can_notify"] is False
    update["update_id"] = 3
    update["message"]["text"] = "   "
    assert client.post("/api/telegram/webhook", json=update, headers=headers).json() == {"ok": True}


def test_legacy_ownerless_subscriptions_stay_private(client):
    conn = db.connect()
    conn.execute("INSERT INTO subscriptions(name,filters,created_at) VALUES ('legacy','{}','2026-01-01')")
    conn.commit()
    conn.close()
    assert client.get("/api/subscriptions", headers=auth()).json()["subscriptions"] == []
