import hashlib
import hmac
import json
import time
from datetime import date, datetime, timedelta, timezone
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
    monkeypatch.delenv("TOURFINDER_DEMO", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("TOUR_ARCHIVE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("APP_ARCHIVE_KEY", raising=False)
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


def seed_offer(*, board="AI", board_name="All inclusive", price=80_000,
               observed_at=None, last_seen_at=None, history=True):
    """Uses only the explicit temporary DATABASE_URL supplied by client."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.connect()
    try:
        conn.execute("INSERT INTO hotels(source,source_hotel_id,name) VALUES ('joinup','h1','Fixture Hotel') ON CONFLICT(source,source_hotel_id) DO NOTHING")
        number = conn.execute("SELECT count(*) FROM offers").scalar() + 1
        offer_id = conn.execute("""INSERT INTO offers(source,source_hotel_id,origin_id,origin_name,
            date_start,date_end,nights,board_code,board_name,room_code,room_name,room_placement,
            pax_adl,pax_chd,children_ages,first_seen_at,last_seen_at,link)
            VALUES ('joinup','h1','RIX','Riga',:start,:finish,7,:board,:board_name,:room,'Standard','2AD',
            2,0,'',:now,:seen,'https://example.test/tour') RETURNING id""", {
                "start": payload()["filters"]["date_from"],
                "finish": (date.fromisoformat(payload()["filters"]["date_from"]) + timedelta(days=7)).isoformat(),
                "board": board, "board_name": board_name, "room": str(number),
                "now": now, "seen": last_seen_at or now,
            }).scalar()
        if history:
            conn.execute("""INSERT INTO price_snapshots(offer_id,fetched_at,price_cents,currency,is_hot)
                VALUES (:id,:when,:price,'EUR',0)""",
                {"id": offer_id, "when": observed_at or now, "price": price})
        conn.commit()
        return offer_id
    finally:
        conn.close()


def test_options_require_approval_and_return_known_meals(client):
    seed_offer(board="HB", board_name="Half board")
    assert client.get("/api/options").status_code == 401
    assert client.get("/api/options", headers=auth(789)).status_code == 403
    response = client.get("/api/options", headers=auth())
    assert response.status_code == 200
    assert response.json()["boards"] == [{"board_code": "HB", "board_name": "Half board"}]


def test_saved_search_queues_exact_party_once(client):
    item = payload()
    item["filters"].update(adults=3, children_ages="12,5")
    for _ in range(2):
        response = client.post("/api/subscriptions", json=item, headers=auth())
        assert response.status_code == 200 and response.json()["collection_requested"]
    conn = db.connect()
    try:
        assert [row["spec"] for row in conn.execute("SELECT spec FROM pax_requests")] == ["3+2:5,12"]
    finally:
        conn.close()


def test_browsing_outage_does_not_become_empty_success(client, monkeypatch):
    from tourfinder import webapp

    def unavailable():
        raise RuntimeError("private connection diagnostics must stay server-side")

    monkeypatch.setattr(webapp, "get_conn", unavailable)
    assert client.get("/app").status_code == 200
    for path in ("/api/search", "/api/compositions", "/api/options", "/api/offers/1", "/api/offers/1/history"):
        response = client.get(path, params=payload()["filters"] if path == "/api/search" else None, headers=auth())
        assert response.status_code == 503
        assert "private connection" not in response.text


@pytest.mark.parametrize("identifier", ["a_bad", "0", "-1", "9223372036854775808"])
def test_invalid_archive_identifier_is_rejected_after_auth(client, identifier):
    for suffix in ("", "/history"):
        path = "/api/offers/" + identifier + suffix
        assert client.get(path).status_code == 401
        assert client.get(path, headers=auth()).status_code == 400


def test_preview_and_readonly_search_without_bot(client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.setenv("TOURFINDER_DEMO", "1")
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
    assert client.get("/api/search", params={**payload()["filters"], "adults": 0}, headers=auth()).status_code == 400
    assert client.get("/api/drops?children_ages=no", headers=auth()).status_code == 400


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


@pytest.mark.parametrize("category", ["UNKNOWN", "AI,NOT_A_MEAL", "AI'); DROP TABLE offers;--"])
def test_unknown_board_category_rejected_by_public_and_subscription_api(client, category):
    filters = {**payload()["filters"], "board_categories": category}
    assert client.get("/api/search", params=filters, headers=auth()).status_code == 400
    assert client.post("/api/subscriptions", json={**payload(), "filters": filters}, headers=auth()).status_code == 422


def test_board_categories_normalize_saved_filters_and_support_legacy_boards(client):
    selected = seed_offer(board="SOFTAI", board_name="Soft all inclusive", price=80_000)
    seed_offer(board="BB", board_name="Breakfast", price=10_000)
    filters = {**payload()["filters"], "board_categories": " ai, uai, AI ", "boards": "SOFTAI"}
    created = client.post("/api/subscriptions", json={**payload(), "filters": filters}, headers=auth())
    assert created.status_code == 200, created.text
    saved = client.get("/api/subscriptions", headers=auth()).json()["subscriptions"][0]["filters"]
    assert set(saved["board_categories"].split(",")) == {"AI", "UAI"}
    assert len(saved["board_categories"].split(",")) == 2
    assert saved["boards"] == "SOFTAI"
    for group in (True, False):
        response = client.get("/api/search", params={**filters, "group": group, "limit": 1}, headers=auth())
        assert response.status_code == 200, response.text
        rows = response.json()["results"]
        assert [row["offer_id"] for row in rows] == [selected]
        assert rows[0]["board_category"] == "AI"
        assert rows[0]["board_label"]
    conflicting = client.get("/api/search", params={**filters, "boards": "BB"}, headers=auth())
    assert conflicting.status_code == 200 and conflicting.json()["count"] == 0


@pytest.mark.parametrize("category", [None, ""])
def test_absent_or_empty_board_categories_keep_unrestricted_saved_search(client, category):
    item = payload()
    item["filters"]["board_categories"] = category
    assert client.post("/api/subscriptions", json=item, headers=auth()).status_code == 200
    saved = client.get("/api/subscriptions", headers=auth()).json()["subscriptions"][0]["filters"]
    assert saved["board_categories"] is None


def test_public_offer_detail_and_lazy_history_agree_on_latest_snapshot(client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.setenv("TOURFINDER_DEMO", "1")
    offer = seed_offer(board="OT", board_name="SELF CATERING", price=100_000)
    conn = db.connect()
    try:
        timestamp = conn.execute("SELECT fetched_at FROM price_snapshots WHERE offer_id=:id", {"id": offer}).scalar()
        conn.execute("""INSERT INTO price_snapshots(offer_id,fetched_at,price_cents,currency,is_hot)
            VALUES (:id,:when,80000,'EUR',1)""", {"id": offer, "when": timestamp})
        conn.execute("""INSERT INTO price_snapshots(offer_id,fetched_at,price_cents,currency,is_hot)
            VALUES (:id,'2000-01-01T00:00:00Z',50000,'EUR',0)""", {"id": offer})
        conn.commit()
    finally:
        conn.close()
    response = client.get(f"/api/offers/{offer}")
    assert response.status_code == 200, response.text
    detail = response.json()["offer"]
    assert detail["offer_id"] == offer and detail["price_cents"] == 80_000
    assert detail["board_category"] == "RO" and detail["board_label"]
    assert detail["room_placement"] == "2AD" and detail["last_seen_at"]
    assert detail["stale"] is False
    assert "history" not in detail
    history = client.get(f"/api/offers/{offer}/history").json()["history"]
    assert [row["price_cents"] for row in history] == [50_000, 100_000, 80_000]
    assert history[-1]["price_cents"] == detail["price_cents"]
    assert history[-1]["fetched_at"] == detail["fetched_at"]


def test_public_offer_detail_retains_stale_card_and_returns_404_without_history(client):
    stale_offer = seed_offer(last_seen_at="2000-01-01T00:00:00Z", price=70_000)
    unseen_offer = seed_offer(history=False)
    response = client.get(f"/api/offers/{stale_offer}", headers=auth())
    assert response.status_code == 200
    assert response.json()["offer"]["stale"] is True
    assert response.json()["offer"]["price_cents"] == 70_000
    search = client.get("/api/search", params=payload()["filters"], headers=auth())
    assert search.status_code == 200 and search.json()["count"] == 0
    assert client.get(f"/api/offers/{unseen_offer}", headers=auth()).status_code == 404
    assert client.get("/api/offers/999999999", headers=auth()).status_code == 404


def access_record(user=789, status="approved"):
    conn = db.connect()
    try:
        conn.execute("""INSERT INTO telegram_access_requests(user_id,status,first_name,requested_at)
            VALUES (:id,:status,'Fixture approved user','2026-10-09T00:00:00Z')
            ON CONFLICT(user_id) DO UPDATE SET status=excluded.status""", {"id": str(user), "status": status})
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["/api/search", "/api/drops", "/api/compositions", "/api/offers/1", "/api/offers/1/history"])
def test_all_tour_data_requires_approved_session(client, path):
    assert client.get(path, params=payload()["filters"]).status_code == 401
    response = client.get(path, params=payload()["filters"], headers=auth(789))
    assert response.status_code == 403
    assert response.json()["detail"]["access_state"] == "not_requested"
    assert "Fixture" not in response.text


@pytest.mark.parametrize("state", ["pending", "denied"])
def test_session_exposes_only_own_access_state_and_blocks_personal_mutations(client, state):
    access_record(status=state)
    response = client.get("/api/telegram/session", headers=auth(789))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "access_required"
    assert response.json()["detail"]["access_state"] == state
    assert "Fixture approved user" not in response.text
    assert client.post("/api/subscriptions", json=payload(), headers=auth(789)).status_code == 403
    assert client.post("/api/pax-requests", json={"adults": 2}, headers=auth(789)).status_code == 403


def test_approved_user_can_read_and_save_but_revoked_user_loses_access(client):
    access_record()
    session = client.get("/api/telegram/session", headers=auth(789))
    assert session.status_code == 200 and session.json()["is_owner"] is False
    assert client.get("/api/search", params=payload()["filters"], headers=auth(789)).status_code == 200
    created = client.post("/api/subscriptions", json=payload(), headers=auth(789))
    assert created.status_code == 200
    assert client.get("/api/subscriptions", headers=auth()).json()["subscriptions"] == []
    access_record(status="denied")
    assert client.get("/api/subscriptions", headers=auth(789)).status_code == 403
    assert client.get("/api/search", params=payload()["filters"], headers=auth(789)).status_code == 403
    assert client.delete(f"/api/subscriptions/{created.json()['id']}", headers=auth(789)).status_code == 403


def test_owner_session_survives_database_failure_other_users_fail_closed(client, monkeypatch):
    from tourfinder import webapp
    def unavailable():
        raise RuntimeError("private connection details must not be exposed")
    monkeypatch.setattr(webapp, "get_conn", unavailable)
    owner = client.get("/api/telegram/session", headers=auth())
    assert owner.status_code == 200
    assert owner.json()["is_admin"] is True and owner.json()["can_notify"] is False
    assert client.get("/app").status_code == 200
    response = client.get("/api/telegram/session", headers=auth(789))
    assert response.status_code == 503 and "private connection" not in response.text


def test_demo_bypass_requires_explicit_local_no_token_and_never_allows_writes(client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert client.get("/api/compositions").status_code == 503
    monkeypatch.setenv("TOURFINDER_DEMO", "1")
    assert client.get("/api/compositions").status_code == 200
    assert client.post("/api/subscriptions", json=payload()).status_code == 503
    monkeypatch.setenv("VERCEL", "1")
    assert client.get("/api/compositions").status_code == 503
    monkeypatch.delenv("VERCEL")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    assert client.get("/api/compositions").status_code == 401
