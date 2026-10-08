"""Tour browsing and personal APIs for owner-approved Telegram accounts."""
import hmac
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import db, meals, queries, reviews
from .telegram_bot import (InitDataError, access_state, allowed_user_ids, bot_token, now_iso,
                           validate_init_data, webhook_response)

ROOT = Path(__file__).resolve().parent.parent.parent
app = FastAPI(title="tour-finder")
templates = Jinja2Templates(directory=str(ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "static"), check_dir=False), name="static")


def get_conn():
    return db.connect()


def require_user(request: Request) -> dict:
    if not bot_token() or not allowed_user_ids():
        raise HTTPException(503, "Telegram ещё не настроен.")
    try:
        user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""), bot_token())
    except InitDataError:
        raise HTTPException(401, "Открой приложение заново через Telegram.") from None
    if str(user["id"]) in allowed_user_ids():
        return user
    conn = None
    try:
        conn = get_conn()
        state = access_state(conn, user["id"])
    except Exception:
        raise HTTPException(503, "Не удалось проверить доступ. Попробуй позже.") from None
    finally:
        if conn is not None:
            conn.close()
    if state != "approved":
        raise HTTPException(403, {"code": "access_required", "access_state": state,
                                 "message": "Доступ к боту выдаёт владелец. Нажми /start в боте."})
    return user


def demo_browsing() -> bool:
    url = os.environ.get("DATABASE_URL", "")
    return (os.environ.get("TOURFINDER_DEMO") == "1" and not bot_token()
            and not os.environ.get("VERCEL") and (not url or url.startswith("sqlite:")))


def require_browsing_user(request: Request):
    if demo_browsing():
        return None
    return require_user(request)


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date_from: date
    date_till: date
    adults: int = Field(2, ge=1, le=6)
    children_ages: str | None = Field(None, max_length=20)
    nights_min: int = Field(1, ge=1, le=30)
    nights_max: int = Field(30, ge=1, le=30)
    budget_max: int | None = Field(None, ge=1, le=100000)
    boards: str | None = Field(None, max_length=256)
    board_categories: str | None = Field(None, max_length=128)
    countries: str | None = Field(None, max_length=512)
    only_hot: bool = False
    stars_min: int | None = Field(None, ge=1, le=5)

    @model_validator(mode="after")
    def valid_ranges(self):
        if self.date_till < self.date_from or (self.date_till - self.date_from).days > 90:
            raise ValueError("Выбери диапазон дат не длиннее 90 дней.")
        if self.nights_max < self.nights_min:
            raise ValueError("Минимум ночей больше максимума.")
        ages = queries._norm_ages(self.children_ages)
        values = [int(a) for a in ages.split(",") if a]
        if len(values) > 4 or any(a < 0 or a > 17 for a in values):
            raise ValueError("Допустимо до 4 детей, возраст 0–17.")
        self.children_ages = ages or None
        self.board_categories = meals.normalize_categories(self.board_categories)
        return self


class SubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field("Мой поиск", min_length=1, max_length=100)
    filters: SearchFilters
    notify_mode: str = Field("deal", pattern="^(deal|budget|both)$")
    min_drop_pct: float = Field(10, ge=1, le=90)
    min_saving_eur: float = Field(100, ge=1, le=10000)
    min_review_rating: float = Field(4, ge=0, le=5)
    min_review_count: int = Field(20, ge=1, le=100000)

    @model_validator(mode="after")
    def needs_budget(self):
        if self.notify_mode in {"budget", "both"} and not self.filters.budget_max:
            raise ValueError("Для уведомлений по бюджету укажи максимальную цену.")
        if self.filters.date_till < datetime.now(timezone.utc).date():
            raise ValueError("Даты поездки уже прошли.")
        return self


class SubscriptionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=100)
    enabled: bool | None = None


@app.get("/")
def index():
    return RedirectResponse("/app")


@app.get("/app")
def miniapp(request: Request):
    conn = None
    try:
        conn = get_conn()
        countries = [dict(r) for r in conn.execute(
            "SELECT DISTINCT country_id,country_name FROM hotels WHERE country_id IS NOT NULL ORDER BY country_name")]
        boards = [dict(r) for r in conn.execute(
            "SELECT board_code,max(board_name) AS board_name FROM offers GROUP BY board_code ORDER BY board_code")]
    except Exception:
        countries, boards = [], []
    finally:
        if conn is not None:
            conn.close()
    return templates.TemplateResponse(request, "miniapp.html", {
        "countries": countries, "boards": boards,
        "telegram_bot_username": os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@"),
        "telegram_enabled": bool(bot_token() and allowed_user_ids()),
        "demo_mode": demo_browsing(),
    })


@app.get("/api/search", dependencies=[Depends(require_browsing_user)])
def search(request: Request, date_from: date, date_till: date,
           adults: int = 2, children_ages: str | None = None,
           nights_min: int = 1, nights_max: int = 30, budget_max: int | None = None,
           boards: str | None = None, countries: str | None = None,
           board_categories: str | None = Query(None, max_length=128),
           only_hot: bool = False, stars_min: int | None = None,
           hotel_id: str | None = Query(None, max_length=128),
           source: str | None = Query(None, max_length=32),
           sort: str = "price", group: bool = True, limit: int = Query(100, ge=1, le=100)):
    try:
        filters = SearchFilters(date_from=date_from, date_till=date_till, adults=adults,
            children_ages=children_ages, nights_min=nights_min, nights_max=nights_max,
            budget_max=budget_max, boards=boards, board_categories=board_categories, countries=countries,
            only_hot=only_hot, stars_min=stars_min).model_dump(mode="json")
    except (ValidationError, ValueError):
        raise HTTPException(400, "Проверь даты, состав туристов и фильтры.") from None
    conn = get_conn()
    try:
        if group and not hotel_id:
            rows = queries.search_hotels_grouped(conn, sort=sort, source=source, limit=limit, **filters)
        else:
            rows = queries.search_offers(conn, sort=sort, source=source, hotel_id=hotel_id, limit=limit, **filters)
        compositions = queries.available_compositions(conn) if not rows else None
    finally:
        conn.close()
    for row in rows:
        row["star_gap"] = reviews.star_gap(row.get("category"), row.get("review_rating"), row.get("review_scale"))
    # Public GETs never schedule paid/external collection work.
    return {"count": len(rows), "results": rows, "available_compositions": compositions, "queued_spec": None}


@app.get("/api/drops", dependencies=[Depends(require_browsing_user)])
def drops(adults: int = Query(2, ge=1, le=6), children_ages: str | None = Query(None, max_length=20),
          hours: int = Query(72, ge=1, le=72), limit: int = Query(100, ge=1, le=100)):
    try:
        ages = queries._norm_ages(children_ages)
        values = [int(a) for a in ages.split(",") if a]
        if len(values) > 4 or any(not 0 <= a <= 17 for a in values):
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "Проверь возраст детей.") from None
    now = datetime.now(timezone.utc)
    conn = get_conn()
    try:
        rows = queries.price_drops(conn, adults=adults, children_ages=ages,
            since=(now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            today=now.date().isoformat(), limit=limit, source="joinup")
        return {"count": len(rows), "results": rows}
    finally:
        conn.close()


@app.get("/api/compositions", dependencies=[Depends(require_browsing_user)])
def compositions():
    conn = get_conn()
    try:
        return {"compositions": queries.available_compositions(conn)}
    finally:
        conn.close()


@app.get("/api/offers/{offer_id}/history", dependencies=[Depends(require_browsing_user)])
def offer_history(offer_id: int):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT fetched_at,price_cents,currency,is_hot,availability FROM price_snapshots WHERE offer_id=:id ORDER BY fetched_at DESC,id DESC LIMIT 2000",
            {"id": offer_id}).fetchall()
        offer = conn.execute("SELECT last_seen_at,date_start FROM offers WHERE id=:id", {"id": offer_id}).fetchone()
        gone = bool(offer and offer["date_start"] >= datetime.now(timezone.utc).date().isoformat()
                    and offer["last_seen_at"] < queries._fresh_cutoff())
        return {"offer_id": offer_id, "gone": gone, "last_seen_at": offer["last_seen_at"] if offer else None,
                "history": [dict(r) for r in reversed(rows)]}
    finally:
        conn.close()


@app.get("/api/offers/{offer_id}", dependencies=[Depends(require_browsing_user)])
def get_offer_detail(offer_id: int):
    conn = get_conn()
    try:
        offer = queries.offer_detail(conn, offer_id)
        if offer is None:
            raise HTTPException(404, "Предложение не найдено.")
        offer["star_gap"] = reviews.star_gap(offer.get("category"), offer.get("review_rating"), offer.get("review_scale"))
        return {"offer": offer}
    finally:
        conn.close()


@app.post("/api/pax-requests")
def request_pax(payload: dict = Body(...), user: dict = Depends(require_user)):
    try:
        adults = int(payload.get("adults", 0))
        ages = sorted(int(a) for a in (payload.get("children_ages") or []))
        if not 1 <= adults <= 6 or len(ages) > 4 or any(not 0 <= a <= 17 for a in ages):
            raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(400, "Допустимо 1–6 взрослых и до 4 детей 0–17 лет.") from None
    spec = str(adults) + (f"+{len(ages)}:" + ",".join(map(str, ages)) if ages else "")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO pax_requests(spec,created_at) VALUES (:spec,:now) ON CONFLICT(spec) DO UPDATE SET created_at=excluded.created_at",
                     {"spec": spec, "now": now_iso()})
        conn.commit()
        return {"spec": spec, "queued": True}
    finally:
        conn.close()


@app.get("/api/telegram/session")
def session(user: dict = Depends(require_user)):
    owner = str(user["id"]) in allowed_user_ids()
    conn, can_notify = None, False
    try:
        conn = get_conn()
        row = conn.execute("SELECT can_notify FROM telegram_users WHERE user_id=:id", {"id": str(user["id"])}).fetchone()
        can_notify = bool(row and row["can_notify"])
    except Exception:
        if not owner:
            raise HTTPException(503, "Не удалось проверить уведомления. Попробуй позже.") from None
    finally:
        if conn is not None:
            conn.close()
    return {"user": user, "can_notify": can_notify, "is_owner": owner, "is_admin": owner,
            "bot_username": os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")}


@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secret or not hmac.compare_digest(secret, received):
        raise HTTPException(403, "Invalid webhook")
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(413, "Update too large")
    try:
        update = json.loads(raw)
        if not isinstance(update, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid update") from None
    conn = get_conn()
    try:
        return webhook_response(conn, update)
    finally:
        conn.close()


def sub_dict(conn, row):
    result = {key: row[key] for key in ("id", "name", "created_at", "notify_mode", "min_drop_pct", "min_review_rating", "min_review_count")}
    result.update(filters=json.loads(row["filters"]), enabled=bool(row["enabled"]),
                  min_saving_eur=row["min_saving_cents"] / 100,
                  unseen=conn.execute("SELECT count(*) FROM alerts WHERE subscription_id=:id AND seen=0", {"id": row["id"]}).scalar())
    return result


@app.get("/api/subscriptions")
def list_subscriptions(user: dict = Depends(require_user)):
    conn = get_conn()
    try:
        return {"subscriptions": [sub_dict(conn, row) for row in conn.execute(
            "SELECT * FROM subscriptions WHERE owner_id=:owner ORDER BY id DESC", {"owner": str(user["id"])})]}
    finally:
        conn.close()


@app.post("/api/subscriptions")
def create_subscription(payload: SubscriptionCreate, user: dict = Depends(require_user)):
    conn = get_conn()
    try:
        if conn.execute("SELECT count(*) FROM subscriptions WHERE owner_id=:owner", {"owner": str(user["id"])}).scalar() >= 20:
            raise HTTPException(400, "Лимит: 20 сохранённых поисков.")
        retained_id = conn.next_retained_id("subscriptions")
        id_column, id_value = ("id,", ":retained_id,") if retained_id is not None else ("", "")
        sub_id = conn.execute(
            f"""INSERT INTO subscriptions({id_column}name,filters,enabled,created_at,owner_id,notify_mode,min_drop_pct,min_saving_cents,min_review_rating,min_review_count)
               VALUES ({id_value}:name,:filters,1,:now,:owner,:mode,:pct,:saving,:rating,:count) RETURNING id""",
            {"name": payload.name.strip() or "Мой поиск", "filters": payload.filters.model_dump_json(),
             "now": now_iso(), "owner": str(user["id"]), "mode": payload.notify_mode,
             "pct": payload.min_drop_pct, "saving": round(payload.min_saving_eur * 100),
             "rating": payload.min_review_rating, "count": payload.min_review_count, "retained_id": retained_id}).scalar()
        conn.commit()
        return {"id": sub_id, "new_alerts": 0, "evaluation_pending": True}
    finally:
        conn.close()


def owned_sub(conn, sub_id, user):
    if not conn.execute("SELECT id FROM subscriptions WHERE id=:id AND owner_id=:owner",
                        {"id": sub_id, "owner": str(user["id"])}).fetchone():
        raise HTTPException(404, "Поиск не найден.")


@app.patch("/api/subscriptions/{sub_id}")
def update_subscription(sub_id: int, payload: SubscriptionPatch, user: dict = Depends(require_user)):
    conn = get_conn()
    try:
        owned_sub(conn, sub_id, user)
        if payload.enabled is not None:
            conn.execute("UPDATE subscriptions SET enabled=:enabled WHERE id=:id", {"enabled": int(payload.enabled), "id": sub_id})
        if payload.name is not None:
            conn.execute("UPDATE subscriptions SET name=:name WHERE id=:id", {"name": payload.name.strip() or "Мой поиск", "id": sub_id})
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: int, user: dict = Depends(require_user)):
    conn = get_conn()
    try:
        owned_sub(conn, sub_id, user)
        conn.execute("DELETE FROM telegram_deliveries WHERE status NOT IN ('sent','uncertain','sending') AND alert_id IN (SELECT id FROM alerts WHERE subscription_id=:id)", {"id": sub_id})
        conn.execute("DELETE FROM alerts WHERE subscription_id=:id", {"id": sub_id})
        conn.execute("DELETE FROM subscriptions WHERE id=:id", {"id": sub_id})
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/alerts")
@app.post("/api/poll")
def poll(user: dict = Depends(require_user)):
    # Evaluation belongs to the worker, never to a browser polling request.
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT a.*,s.name AS sub_name,h.name AS hotel_name,h.category,h.country_name,
                      h.city_name,o.date_start,o.nights,o.board_code,o.board_name,o.link
               FROM alerts a JOIN subscriptions s ON s.id=a.subscription_id
               JOIN offers o ON o.id=a.offer_id
               JOIN hotels h ON h.source=o.source AND h.source_hotel_id=o.source_hotel_id
               WHERE a.seen=0 AND s.owner_id=:owner ORDER BY a.created_at DESC,a.id DESC LIMIT 200""",
            {"owner": str(user["id"])}).fetchall()
        result = [{**dict(row), "evidence": json.loads(row["evidence"])} for row in rows]
        return {"unseen": len(result), "alerts": result}
    finally:
        conn.close()


@app.post("/api/alerts/seen")
def mark_alerts_seen(payload: dict = Body(...), user: dict = Depends(require_user)):
    ids = payload.get("ids")
    if not isinstance(ids, list) or len(ids) > 200 or any(type(v) is not int or v <= 0 for v in ids):
        raise HTTPException(400, "Передай ids показанных уведомлений (до 200).")
    if not ids:
        return {"ok": True}
    params = {f"i{n}": value for n, value in enumerate(ids)}
    marks = ",".join(f":{key}" for key in params)
    params["owner"] = str(user["id"])
    conn = get_conn()
    try:
        conn.execute(f"UPDATE alerts SET seen=1 WHERE id IN ({marks}) AND subscription_id IN (SELECT id FROM subscriptions WHERE owner_id=:owner)", params)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
