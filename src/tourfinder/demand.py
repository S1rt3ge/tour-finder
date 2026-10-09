"""Owner-free, bounded discovery tasks derived from approved personal demand.

Completion refreshes one exact discovery branch, never source inventory.
Planning only reads; fetch_runs is the existing durable attempt/checkpoint log.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from . import collection_requests, countries, meals
from .sources.waavo_discovery import CHEAP_COUNTRY_IDS
from .telegram_bot import approved_user_ids

DEMAND_CONTRACT = "waavo_discovery_v1"
DISCOVERY_CONTRACT = "filtered_hotel_discovery_v1"
PART_DAYS = 7
MAX_REQUESTS = 8
MAX_SECONDS = 180
RETRY_AFTER = timedelta(minutes=15)
UNSUPPORTED_RETRY_AFTER = timedelta(hours=24)
# Public Waavo filter metadata, inspected 2026-10-09. Join Up is collected
# directly; these branches still describe hotel discovery, not inventory.
OPERATORS = ("anextour", "coral", "novaturas", "teztour")
# This endpoint has a different country namespace from the old catalogue.
# In particular old 18 meant Turkey; discovery 18 means Egypt.
_DISCOVERY_NAMES = {"Spānija": "ES", "Apvienotie Arābu Emirāti": "AE", "Andora": "AD",
    "Lietuva": "LT", "Šri Lanka": "LK", "Tanzānija": "TZ", "Kolumbija": "CO", "Francija": "FR",
    "Taizeme": "TH", "Itālija": "IT", "Indonēzija": "ID", "Horvātija": "HR", "Maurīcija": "MU",
    "Vjetnama": "VN", "Kenija": "KE", "Portugāle": "PT", "Austrija": "AT", "Maroka": "MA"}


@dataclass(frozen=True)
class DemandTask:
    query_key: str
    filters: dict
    origin: str
    pax: str
    meal_group: str | None = None
    country_ids: tuple[str, ...] | None = None
    operator: str | None = None
    unsupported_reasons: tuple[str, ...] = ()
    period_hours: int = 4
    max_requests: int = MAX_REQUESTS
    max_seconds: int = MAX_SECONDS
    source: str = "waavo"
    tier: str = "demand"

    @property
    def key(self):
        return self.source, self.tier, self.query_key, self.origin

    @property
    def scope(self):
        return self.origin, self.pax


def _now(now=None):
    value = now or datetime.now(timezone.utc)
    if value.utcoffset() is None:
        raise ValueError("timezone required")
    return value.astimezone(timezone.utc)


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


def _filters(raw):
    if not isinstance(raw, dict):
        raise ValueError("invalid filters")
    # Validate the original persisted values before normalizing numeric lists.
    parties = collection_requests._party_scopes(raw)
    adults, ages, _ = next(iter(parties))
    result = collection_requests.canonical_filters(raw | {"adults": adults, "children_ages": ages})
    first, last = collection_requests._date_range(result)
    if (last - first).days > 90:
        raise ValueError("invalid dates")
    for name, low, high in (("nights_min", 1, 30), ("nights_max", 1, 30),
                            ("budget_max", 1, 100000), ("stars_min", 1, 5)):
        value = result[name]
        if value is None and name in {"budget_max", "stars_min"}:
            continue
        if type(value) is not int or not low <= value <= high:
            raise ValueError("invalid filter")
    if result["nights_min"] > result["nights_max"] or type(result["only_hot"]) is not bool:
        raise ValueError("invalid filter")
    result["countries"] = countries.normalize_countries(result["countries"])
    result["board_categories"] = meals.normalize_categories(result["board_categories"])
    return collection_requests.canonical_filters(result)


def active_filters(conn, now=None):
    """All valid collectable owned searches, deduplicated without user IDs."""
    now = _now(now)
    owners = approved_user_ids(conn)
    if not owners:
        return []
    records = conn.execute("""SELECT owner_id,filters FROM collection_requests WHERE expires_at>:now
        UNION ALL SELECT owner_id,filters FROM subscriptions WHERE enabled=1 AND owner_id IS NOT NULL""",
        {"now": collection_requests._iso(now)})
    unique = {}
    for row in records:
        if row["owner_id"] not in owners:
            continue
        try:
            filters = _filters(json.loads(row["filters"]))
            if collection_requests._collectable(filters, now):
                unique[collection_requests.request_key(filters)] = filters
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return [unique[key] for key in sorted(unique)]


def query_key(filters, meal_group=None, country_ids=None, operator=None):
    payload = {"contract": DEMAND_CONTRACT, "filters": collection_requests.canonical_filters(filters),
               "meal_group": meal_group, "country_ids": sorted(set(country_ids)) if country_ids else None,
               "operator": operator}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def _country_map(conn):
    mapping = {"country:" + code: {identifier} for code, identifier in CHEAP_COUNTRY_IDS.items()
               if code in countries.COUNTRY_NAMES}
    legacy = {}
    for identifier, code in countries.SOURCE_COUNTRY_IDS.get("waavo", {}).items():
        legacy.setdefault(identifier, set()).add(code)
    for row in conn.execute("SELECT DISTINCT country_id,country_name FROM hotels WHERE source='waavo' AND country_id IS NOT NULL"):
        identifier = str(row["country_id"])
        if not re.fullmatch(r"[1-9][0-9]{0,11}", identifier):
            continue
        name = str(row["country_name"] or "").strip(" ")
        code = countries._name_code(name) or _DISCOVERY_NAMES.get(name)
        # Unknown named rows make a reused numeric ID ambiguous, not trusted.
        if name:
            legacy.setdefault(identifier, set()).add(code)
    for identifier, codes in legacy.items():
        if len(codes) == 1 and CHEAP_COUNTRY_IDS.get(next(iter(codes))) == identifier:
            # Public raw filters intentionally keep exact old IDs; remapping
            # old 18 to new 15 would fetch rows the user's SQL rejects.
            mapping[identifier] = {identifier}
    return mapping


def _country_branch(value, mapping):
    if not value:
        return None, ()
    selected = value.split(",")
    if any(item not in mapping for item in selected):
        return None, ("country_unmapped",)
    return tuple(sorted(set().union(*(mapping[item] for item in selected)))), ()


def _meal_branches(filters):
    categories = set(filters["board_categories"].split(",")) if filters["board_categories"] else None
    if filters["boards"]:
        aliases = {code.upper(): category for category, codes in meals._CODES.items() for code in codes}
        raw = filters["boards"].split(",")
        if any(code.upper() not in aliases for code in raw):
            return [(None, ("raw_board_unmapped",))]
        raw_categories = {aliases[code.upper()] for code in raw}
        categories = raw_categories if categories is None else categories & raw_categories
    if categories is None:
        return [(None, ())]
    if not categories:
        return [(None, ("meal_filter_empty",))]
    # Official UI labels these four choices "and better". Use one supported
    # lower bound, preserving the original exact raw/category AND locally.
    thresholds = {"RO": 0, "BB": 1, "HB": 2, "FB": 2, "AI": 3, "UAI": 3}
    supported = categories & thresholds.keys()
    branches = [(("RO", "BB", "HB", "AI")[min(thresholds[item] for item in supported)], ())] if supported else []
    if "OTHER" in categories:
        branches.append((None, ("meal_category_unsupported",)))
    return branches


def tasks_for(conn, filters_list, now=None):
    """Calendar-aligned parts keep interior keys stable as tomorrow advances."""
    now = _now(now)
    tomorrow, horizon = now.date() + timedelta(days=1), now.date() + timedelta(days=45)
    country_map = _country_map(conn)
    tasks = {}
    for filters in filters_list:
        first, last = collection_requests._date_range(filters)
        first, last = max(first, tomorrow), min(last, horizon)
        if first > last:
            continue
        low, high = max(2, filters["nights_min"]), min(21, filters["nights_max"])
        reasons = ("nights_unsupported",) if low > high else ()
        if filters["only_hot"]:
            reasons += ("hot_filter_unsupported",)
        country_ids, country_reasons = _country_branch(filters["countries"], country_map)
        ages = filters["children_ages"] or ""
        pax = str(filters["adults"]) + (f"+{len(ages.split(','))}:" + ages if ages else "")
        start = first
        while start <= last:
            # date.toordinal() starts on Monday; anchor boundaries independently
            # of the current day so completed future parts do not move daily.
            end = min(last, start + timedelta(days=6 - (start.toordinal() - 1) % PART_DAYS))
            period = 4 if (start - now.date()).days <= 7 else 12 if (start - now.date()).days <= 14 else 24
            for origin in filters["origins"].split(","):
                part = filters | {"origins": origin, "date_from": start.isoformat(), "date_till": end.isoformat()}
                if low <= high:
                    part |= {"nights_min": low, "nights_max": high}
                for meal_group, meal_reasons in _meal_branches(filters):
                    for operator in OPERATORS:
                        key = query_key(part, meal_group, country_ids, operator)
                        tasks[key] = DemandTask(query_key=key, filters=part, origin=origin, pax=pax,
                            meal_group=meal_group, country_ids=country_ids, operator=operator,
                            unsupported_reasons=tuple(dict.fromkeys(reasons + country_reasons + meal_reasons)),
                            period_hours=period)
            start = end + timedelta(days=1)
    return sorted(tasks.values(), key=lambda task: (task.filters["date_from"], task.scope, task.query_key))


def run_history(conn, now=None):
    now = _now(now)
    history = {}
    for row in conn.execute("SELECT started_at,finished_at,errors,params FROM fetch_runs WHERE tier='demand' ORDER BY id"):
        try:
            params = json.loads(row["params"])
            key = params["query_key"]
            if (params.get("source") != "waavo" or params.get("demand_contract") != DEMAND_CONTRACT
                    or not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None):
                continue
            # Ignore mismatched hashes rather than refreshing a different query.
            if query_key(params["filters"], params.get("meal_group"), params.get("country_ids"), params.get("operator")) != key:
                continue
            started = _time(row["started_at"])
            if started > now:
                continue
            record = history.setdefault(key, {"attempted": None, "succeeded": None, "unsupported": False})
            if record["attempted"] is None or started >= record["attempted"]:
                record.update(attempted=started, unsupported=bool(params.get("unsupported_reasons")))
            errors = json.loads(row["errors"]) if row["errors"] is not None else []
            if (not row["finished_at"] or errors != [] or params.get("discovery_complete") is not True
                    or params.get("discovery_contract") != DISCOVERY_CONTRACT or params.get("unsupported_reasons")):
                continue
            finished = _time(row["finished_at"])
            if started <= finished <= now and (record["succeeded"] is None or finished > record["succeeded"]):
                record["succeeded"] = finished
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return history


def due_tasks(tasks, history, now=None):
    now = _now(now)
    due = []
    for task in tasks:
        record = history.get(task.query_key, {})
        success, attempted = record.get("succeeded"), record.get("attempted")
        if success and now - success < timedelta(hours=task.period_hours):
            continue
        backoff = UNSUPPORTED_RETRY_AFTER if record.get("unsupported") else RETRY_AFTER
        if attempted and now - attempted < backoff:
            continue
        due.append(task)
    return fair_order(due, history, all_tasks=tasks)


def fair_order(tasks, history, *, all_tasks=None):
    """One oldest branch per airport/party before a second branch of a scope."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    groups = {}
    for task in tasks:
        groups.setdefault(task.scope, []).append(task)
    for group in groups.values():
        group.sort(key=lambda task: (history.get(task.query_key, {}).get("attempted") or epoch,
                                     task.filters["date_from"], task.query_key))
    # Recently served scopes wait behind scopes never served, even if each has
    # many new date/meal branches with no individual attempt yet.
    served = {scope: epoch for scope in groups}
    for task in all_tasks if all_tasks is not None else tasks:
        if task.scope in served:
            served[task.scope] = max(served[task.scope], history.get(task.query_key, {}).get("attempted") or epoch)
    order = sorted(groups, key=lambda scope: (served[scope], scope))
    result = []
    while any(groups.values()):
        for scope in order:
            if groups[scope]:
                result.append(groups[scope].pop(0))
    return result


def plan_demand(conn, now=None):
    now = _now(now)
    return due_tasks(tasks_for(conn, active_filters(conn, now), now), run_history(conn, now), now)
