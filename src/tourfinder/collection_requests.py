"""Bounded, owner-scoped demand. A saved request is never proof of collection."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re

from .origins import normalize_origins
from .telegram_bot import approved_user_ids, has_access

MAX_OWNER_REQUESTS = 20
MAX_ACTIVE_ORIGIN_PARTIES = 12
REQUEST_TTL = timedelta(days=21)
REFRESH_COOLDOWN = timedelta(minutes=15)
_MATCHING = {"date_from", "date_till", "adults", "children_ages", "origins",
             "nights_min", "nights_max", "budget_max", "boards", "board_categories",
             "countries", "only_hot", "stars_min"}
_DEFAULTS = dict(adults=2, children_ages=None, origins="RIX", nights_min=1, nights_max=30,
                 budget_max=None, boards=None, board_categories=None, countries=None,
                 only_hot=False, stars_min=None)


class RequestLimit(ValueError):
    pass


def _now(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_filters(filters):
    """Call after API validation; normalize set order for exact idempotency."""
    result = _DEFAULTS | {key: value for key, value in filters.items() if key in _MATCHING}
    result["origins"] = normalize_origins(result.get("origins"))
    for key in ("boards", "board_categories", "countries"):
        result[key] = ",".join(sorted({value.strip() for value in (result.get(key) or "").split(",") if value.strip()})) or None
    ages = sorted(int(value) for value in str(result.get("children_ages") or "").split(",") if value.strip())
    result["children_ages"] = ",".join(map(str, ages)) or None
    return result


def request_key(filters):
    return hashlib.sha256(json.dumps(canonical_filters(filters), ensure_ascii=True,
                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def lock_owner(conn, owner):
    """Serialize a user's subscription quota before COUNT/INSERT on both DBs."""
    name = "subscription_owner_" + hashlib.sha256(str(owner).encode()).hexdigest()
    conn.execute("INSERT INTO id_counters(name,last_id) VALUES (:name,0) ON CONFLICT(name) DO NOTHING", {"name": name})
    conn.execute("UPDATE id_counters SET last_id=last_id WHERE name=:name", {"name": name})


def active_scopes(conn, now=None):
    """Only approved owners' nonexpired durable requests activate collection."""
    current = _now(now)
    approved = approved_user_ids(conn)
    if not approved:
        return []
    result = []
    for row in conn.execute("SELECT owner_id,filters FROM collection_requests WHERE expires_at>:now",
                            {"now": _iso(current)}):
        if row["owner_id"] not in approved:
            continue
        try:
            filters = canonical_filters(json.loads(row["filters"]))
            _party_scopes(filters)
            if _collectable(filters, current):
                result.append(filters)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return result


def _party_scopes(filters):
    """Match the collector's persisted party validation, without lossy casts."""
    def number(value, low, high):
        if type(value) is not int and not (isinstance(value, str) and re.fullmatch(r"[0-9]{1,2}", value.strip())):
            raise ValueError("invalid party")
        value = int(value)
        if not low <= value <= high:
            raise ValueError("invalid party")
        return value

    adults = number(filters["adults"], 1, 6)
    ages = filters.get("children_ages")
    if ages is None or ages == "":
        ages = []
    elif isinstance(ages, str):
        ages = ages.split(",")
    if not isinstance(ages, (list, tuple)) or len(ages) > 4:
        raise ValueError("invalid party")
    ages = ",".join(map(str, sorted(number(age, 0, 17) for age in ages)))
    return {(adults, ages, origin) for origin in normalize_origins(filters.get("origins")).split(",")}


def _date_range(filters):
    first, last = date.fromisoformat(filters["date_from"]), date.fromisoformat(filters["date_till"])
    if first > last:
        raise ValueError("invalid dates")
    return first, last


def _collectable(filters, current):
    first, last = _date_range(filters)
    return (last >= current.date() + timedelta(days=1)
            and first <= current.date() + timedelta(days=45))


def _bootstrap_scopes(conn, current):
    scopes = {(2, "", "RIX"), (3, "", "RIX"), (2, "7", "RIX")}
    for row in conn.execute("SELECT spec FROM pax_requests WHERE created_at>=:cutoff AND created_at<=:now",
                            {"cutoff": _iso(current - REQUEST_TTL), "now": _iso(current)}):
        spec = row["spec"]
        if (not isinstance(spec, str) or len(spec) > 128
                or not re.fullmatch(r"[0-9]{1,2}(?:\+[0-4]:(?:[0-9]{1,2}(?: *, *[0-9]{1,2})*)?)?", spec.strip())):
            continue
        adult, _, children = spec.strip().partition("+")
        count, _, values = children.partition(":")
        ages = values.split(",") if values else []
        if children and int(count) != len(ages):
            continue
        try:
            scopes.update(_party_scopes({"adults": adult, "children_ages": ages, "origins": "RIX"}))
        except ValueError:
            continue
    return scopes


def admit_scope(conn, owner, filters, now=None):
    """Reserve capacity for an enabled search, including future subscriptions.

    Hold this short global lock until the caller commits its request or saved
    search. Future reservations do not claim that collection has been queued.
    Bootstrap scopes apply only without current owned demand; a proposed
    collectable request also replaces bootstrap during its own admission.
    """
    current = _now(now)
    filters = canonical_filters(filters)
    owner = str(owner)
    # Global capacity check and insert share one transaction/row lock. This is
    # a policy bound, not a throughput or completion-time guarantee.
    conn.execute("INSERT INTO id_counters(name,last_id) VALUES ('collection_request_admission_v1',0) ON CONFLICT(name) DO NOTHING")
    conn.execute("UPDATE id_counters SET last_id=last_id WHERE name='collection_request_admission_v1'")
    if not has_access(conn, owner):
        raise RequestLimit("Доступ к сбору не одобрен.")
    scopes = set()
    owned_collectable = _collectable(filters, current)
    for active in active_scopes(conn, current):
        scopes.update(_party_scopes(active))
        owned_collectable = True
    approved = approved_user_ids(conn)
    for row in conn.execute("SELECT owner_id,filters FROM subscriptions WHERE enabled=1 AND owner_id IS NOT NULL"):
        if row["owner_id"] not in approved:
            continue
        try:
            active = json.loads(row["filters"])
            active_party = _party_scopes(active)
            if _date_range(active)[1] >= current.date():
                scopes.update(active_party)
                owned_collectable = owned_collectable or _collectable(active, current)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    requested = _party_scopes(filters)
    if not owned_collectable:
        scopes.update(_bootstrap_scopes(conn, current))
    if requested - scopes and len(scopes | requested) > MAX_ACTIVE_ORIGIN_PARTIES:
        raise RequestLimit("Очередь новых составов и аэропортов заполнена. Поиск по собранным данным доступен; сбор можно запросить позже.")


def queue_request(conn, owner, filters, now=None):
    """Short atomic admission; caller commits together with any subscription."""
    current = _now(now)
    filters = canonical_filters(filters)
    owner = str(owner)
    admit_scope(conn, owner, filters, current)
    key = request_key(filters)
    existing = conn.execute("SELECT * FROM collection_requests WHERE owner_id=:owner AND request_key=:key",
                            {"owner": owner, "key": key}).fetchone()
    if existing and existing["expires_at"] > _iso(current) and existing["updated_at"] > _iso(current - REFRESH_COOLDOWN):
        return dict(existing)
    if not existing or existing["expires_at"] <= _iso(current):
        count = conn.execute("SELECT count(*) AS total FROM collection_requests WHERE owner_id=:owner AND expires_at>:now",
                             {"owner": owner, "now": _iso(current)}).fetchone()["total"]
        if count >= MAX_OWNER_REQUESTS:
            raise RequestLimit("Лимит: 20 активных заявок на сбор. Повтори один из уже сохранённых поисков.")
    values = {"owner": owner, "key": key, "filters": json.dumps(filters, ensure_ascii=False),
              "now": _iso(current), "expires": _iso(current + REQUEST_TTL)}
    row = conn.execute("""INSERT INTO collection_requests(owner_id,request_key,filters,created_at,updated_at,expires_at)
        VALUES (:owner,:key,:filters,:now,:now,:expires)
        ON CONFLICT(owner_id,request_key) DO UPDATE SET updated_at=excluded.updated_at,expires_at=excluded.expires_at
        RETURNING *""", values).fetchone()
    return dict(row)


def matching_request(conn, owner, filters, now=None):
    if owner is None:
        return None
    row = conn.execute("""SELECT id,updated_at FROM collection_requests
        WHERE owner_id=:owner AND request_key=:key AND expires_at>:now""",
        {"owner": str(owner), "key": request_key(filters), "now": _iso(_now(now))}).fetchone()
    return dict(row) if row else None
