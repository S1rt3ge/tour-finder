"""Read-only evidence about collection coverage, separate from offer inventory.

Stable states: fresh, running, queued, partial, stale, uncollected, unsupported.
The caller may use unavailable when SQL reads fail; database errors propagate.
Only explicit source/origin/party/date/night metadata from successful uncapped
runs contributes coverage. In particular legacy Join Up runs without recorded
stays, and Waavo runs without recorded origin/duration bounds plus the exact
response-validation and bounded-inventory contract versions, cannot prove an
empty search. Local price, meal and star filters never relax collection scope.

Intervals are inclusive departure dates for which *every requested night count*
was checked. last_complete_at is the latest validated completed contributing
run, not a replacement for complete. queue.requested means a recent composition
request, not a job for these exact filters. Running is only a recent recorded
start (with an owner and no superseding run); it is not proof of a live process.
No personal IDs, run IDs, raw errors or owner metadata are returned.
"""
from datetime import date, datetime, timedelta, timezone
import json
import re

from .origins import normalize_origins, normalize_source_origin

SOURCES = ("joinup", "waavo")
# Current collector policy; kept local to avoid importing the CLI into web reads.
TIERS = (("near", 1, 7, 4), ("mid", 8, 14, 12), ("far", 15, 45, 24))
HISTORY_LIMIT = 2000
RUNNING_MAX_AGE = timedelta(minutes=15)
REQUEST_MAX_AGE = timedelta(days=21)
WAAVO_SCOPE_VALIDATION = "response_date_nights_v1"
WAAVO_INVENTORY_CONTRACT = "bounded_inventory_v1"


def _time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _day(value):
    return value if type(value) is date else date.fromisoformat(value)


def _number(value, low, high):
    if type(value) is not int and not (isinstance(value, str) and re.fullmatch(r"[0-9]{1,2}", value)):
        raise ValueError("integer")
    value = int(value)
    if not low <= value <= high:
        raise ValueError("range")
    return value


def _party(adults, ages):
    adult = _number(adults, 1, 6)
    if ages is None or ages == "":
        ages = []
    if isinstance(ages, str):
        ages = ages.split(",")
    if not isinstance(ages, (list, tuple)) or len(ages) > 4:
        raise ValueError("ages")
    ages = sorted(_number(age.strip() if isinstance(age, str) else age, 0, 17) for age in ages)
    return str(adult) + (f"+{len(ages)}:" + ",".join(map(str, ages)) if ages else "")


def _spec(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("party")
    match = re.fullmatch(r"([1-6])(?:\+([0-4]):([0-9, ]*))?", value)
    if not match:
        raise ValueError("party")
    ages = match[3].split(",") if match[3] else []
    if match[2] is not None and int(match[2]) != len(ages):
        raise ValueError("party")
    return _party(match[1], ages)


def _object(value):
    if not isinstance(value, str) or len(value) > 65536:
        raise ValueError("metadata")
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("metadata")
    return result


def _intervals(days):
    result = []
    for day in sorted(set(days)):
        if result and day == date.fromisoformat(result[-1]["date_till"]) + timedelta(days=1):
            result[-1]["date_till"] = day.isoformat()
        else:
            result.append({"date_from": day.isoformat(), "date_till": day.isoformat()})
    return result


def _owner(params):
    owner = params.get("collector_owner")
    if not isinstance(owner, dict):
        return None
    values = tuple(owner.get(key) for key in
        ("GITHUB_REPOSITORY", "GITHUB_WORKFLOW", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    if not values[2].isdecimal() or not values[3].isdecimal():
        return None
    return values


def _run_nights(params, source):
    if source == "waavo":
        first = _number(params.get("durationFrom"), 1, 30)
        last = _number(params.get("durationTo"), 1, 30)
        if first > last:
            raise ValueError("duration")
        return set(range(first, last + 1))
    stays = params.get("stays")
    if isinstance(stays, str):
        stays = stays.split(",")
    if not isinstance(stays, (list, tuple)) or not stays or len(stays) > 30:
        raise ValueError("stays")
    return {_number(value, 1, 30) for value in stays}


def get_search_coverage(conn, filters, now=None, queue_override=None):
    """Return additive JSON collection evidence; execute SELECTs only.

    Read at most 2001 run records. A truncated history cannot create coverage;
    sufficiently recent positive evidence can still prove the requested scope.
    SQL failures deliberately propagate so the caller can roll back its read
    connection and return coverage unavailable without hiding search results.
    queue_override is already owner-authorized exact-search evidence supplied
    by the caller. None retains legacy Riga composition requests; an explicit
    false override suppresses them. Only the four public request-reference
    fields are retained, never owner IDs or unrelated private row contents.
    """
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    today = now.date()
    horizon_first = today + timedelta(days=min(tier[1] for tier in TIERS))
    horizon_last = today + timedelta(days=max(tier[2] for tier in TIERS))
    result = {"state": "uncollected", "complete": False, "last_complete_at": None,
              "horizon": {"date_from": horizon_first.isoformat(), "date_till": horizon_last.isoformat()},
              "queue": {"requested": False, "requested_at": None}, "sources": [], "reasons": []}
    if queue_override is not None:
        if not isinstance(queue_override, dict) or type(queue_override.get("requested")) is not bool:
            raise ValueError("invalid queue override")
        result["queue"]["requested"] = queue_override["requested"]
        if queue_override.get("scope") == "exact_search":
            result["queue"]["scope"] = "exact_search"
        if queue_override["requested"]:
            if queue_override.get("requested_at") is not None:
                result["queue"]["requested_at"] = _iso(_time(queue_override["requested_at"]))
            request_id = queue_override.get("request_id")
            if type(request_id) is int and request_id > 0:
                result["queue"]["request_id"] = request_id
    try:
        first, last = _day(filters["date_from"]), _day(filters["date_till"])
        if first > last or (last - first).days > 90:
            raise ValueError("dates")
        party = _party(filters.get("adults", 2), filters.get("children_ages"))
        night_first = _number(filters.get("nights_min", 1), 1, 30)
        night_last = _number(filters.get("nights_max", 30), 1, 30)
        if night_first > night_last:
            raise ValueError("nights")
        source_filter = filters.get("source")
        if source_filter is not None and source_filter not in SOURCES:
            raise ValueError("source")
    except (ValueError, TypeError, KeyError, OverflowError):
        result.update(state="unsupported", reasons=["invalid_search_scope"])
        return result
    selected_sources = [source_filter] if source_filter else list(SOURCES)
    try:
        origin_filter = filters.get("origins")
        if origin_filter is None and ("origin_id" in filters or "origin" in filters):
            origin_filter = normalize_source_origin("joinup", filters.get("origin_id", filters.get("origin")))
            if origin_filter is None:
                raise ValueError("origin")
        selected_origins = normalize_origins(origin_filter).split(",")
    except (ValueError, TypeError):
        result.update(state="unsupported", reasons=["origin_unsupported"])
        return result
    days = {first + timedelta(days=offset) for offset in range((last - first).days + 1)}
    supported_days = {day for day in days if horizon_first <= day <= horizon_last}
    nights = set(range(night_first, night_last + 1))
    global_reasons = []
    if supported_days != days:
        global_reasons.append("dates_outside_horizon")
    if (filters.get("room_count", 1) != 1 or filters.get("rooms")
            or filters.get("room_placement") or filters.get("room_code")):
        global_reasons.append("rooms_unsupported")

    # Queue writes canonicalize ages; use the unique composition key, not an
    # inventory-wide scan. A malformed legacy alias cannot establish a request.
    # Legacy anonymous composition requests scheduled Riga only. The authenticated
    # caller can instead supply owner-scoped evidence before states are computed.
    queue_rows = (conn.execute("SELECT spec,created_at FROM pax_requests WHERE spec=:spec", {"spec": party}).fetchall()
                  if queue_override is None and selected_origins == ["RIX"] else [])
    for row in queue_rows:
        try:
            requested = _time(row["created_at"])
            if _spec(row["spec"]) == party and timedelta(0) <= now - requested <= REQUEST_MAX_AGE:
                if not result["queue"]["requested_at"] or requested > _time(result["queue"]["requested_at"]):
                    result["queue"] = {"requested": True, "requested_at": _iso(requested)}
        except (ValueError, TypeError, KeyError):
            continue
    records = conn.execute("""SELECT id,started_at,finished_at,tier,pax_spec,params,errors
        FROM fetch_runs ORDER BY id DESC LIMIT :limit""", {"limit": HISTORY_LIMIT + 1}).fetchall()
    clipped = len(records) > HISTORY_LIMIT
    records = records[:HISTORY_LIMIT]
    parsed, latest_owners = [], {}
    for row in records:
        try:
            params = _object(row["params"])
            owner = _owner(params)
            if owner:
                latest_owners.setdefault(owner[:2], row["id"])
            if _spec(row["pax_spec"]) != party or _party(params["adults"], params.get("children_ages")) != party:
                continue
            source = params.get("source")
            if source not in selected_sources:
                continue
            parsed.append((row, params, source, owner))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue

    for source, origin_code in ((source, origin) for source in selected_sources for origin in selected_origins):
        reasons = list(global_reasons)
        allowed_nights = set(range(2, 22)) if source == "waavo" else set(range(1, 31))
        if not nights <= allowed_nights:
            reasons.append("nights_outside_source_range")
        fresh_cells, historic_cells = set(), set()
        completed_at, running = None, False
        for row, params, row_source, owner in parsed:
            if row_source != source:
                continue
            try:
                start = _time(row["started_at"])
                if start > now:
                    raise ValueError("future run")
                origin = params.get("origin") if source == "joinup" else params.get("departureAirport")
                if origin is None:
                    reasons.append("missing_run_metadata")
                    continue
                if normalize_source_origin(source, origin) != origin_code:
                    continue
                if source == "joinup":
                    bounds = params["dates"].split(":")
                    if len(bounds) == 1:
                        bounds *= 2
                    run_first, run_last = map(_day, bounds)
                else:
                    run_first, run_last = _day(params["dateFrom"]), _day(params["dateTo"])
                if run_first > run_last:
                    raise ValueError("dates")
                overlap = {day for day in supported_days if run_first <= day <= run_last}
                if not overlap:
                    continue
                # Hotel discovery returns selected minimum-price variants,
                # not all rooms/dates. Even exhausted, exact demand parts
                # must never fill inventory cells or claim a broad search is
                # running, regardless of any accidental inventory marker.
                if row["tier"] == "demand" or params.get("discovery_contract"):
                    reasons.append("filtered_discovery_only")
                    continue
                errors = json.loads(row["errors"]) if row["errors"] is not None else []
                if "max_pages" not in params:
                    reasons.append("missing_run_metadata")
                    continue
                cap = params["max_pages"]
                if errors != [] or not (cap is None or type(cap) is int and cap == 0):
                    reasons.append("incomplete_run")
                    continue
                # A restricted source query cannot prove an unrestricted search.
                if any(params.get(key) for key in ("destinations", "country", "countries", "hotel_ids",
                       "boards", "mealGroupFrom", "stars", "room_count", "rooms", "tour_types")):
                    reasons.append("restricted_run_scope")
                    continue
                if source == "joinup" and "destinations" not in params:
                    reasons.append("missing_run_metadata")
                    continue
                if row["finished_at"] is None:
                    reasons.append("running_unconfirmed")
                    if owner and now - start <= RUNNING_MAX_AGE and latest_owners.get(owner[:2]) == row["id"]:
                        running = True
                    continue
                finish = _time(row["finished_at"])
                if not start <= finish <= now:
                    raise ValueError("finish")
                # Earlier Waavo collectors recorded requested bounds without
                # verifying returned dates/nights. Their successful exit alone
                # cannot contribute either fresh or historical coverage cells.
                if source == "waavo" and params.get("scope_validation") != WAAVO_SCOPE_VALIDATION:
                    reasons.append("response_scope_unverified")
                    continue
                # Validated rows (including zero rows) are not proof that the
                # endpoint exhaustively searched the requested bounds. The
                # current legacy catalogue adapter never records this contract.
                if source == "waavo" and params.get("inventory_contract") != WAAVO_INVENTORY_CONTRACT:
                    reasons.append("inventory_contract_unverified")
                    continue
                try:
                    run_nights = _run_nights(params, source)
                except (ValueError, TypeError):
                    reasons.append("stays_not_recorded" if source == "joinup" else "missing_run_metadata")
                    continue
                covered_nights = nights & run_nights & allowed_nights
                if not covered_nights:
                    continue
                completed_at = max(completed_at, finish) if completed_at else finish
                for day in overlap:
                    cells = {(day, night) for night in covered_nights}
                    historic_cells.update(cells)
                    cadence = next(hours for _, low, high, hours in TIERS
                                   if low <= (day - today).days <= high)
                    if now - finish <= timedelta(hours=cadence):
                        fresh_cells.update(cells)
            except (ValueError, TypeError, KeyError, AttributeError):
                reasons.append("missing_run_metadata")
        covered_days = {day for day in supported_days if all((day, night) in fresh_cells for night in nights)}
        complete = covered_days == days and not global_reasons and nights <= allowed_nights
        if not supported_days or "origin_unsupported" in reasons or "rooms_unsupported" in reasons or not nights & allowed_nights:
            state = "unsupported"
        elif complete:
            state = "fresh"
        elif global_reasons or "nights_outside_source_range" in reasons:
            state = "partial"
        elif running:
            state = "running"
        elif result["queue"]["requested"]:
            state = "queued"
        elif fresh_cells or reasons:
            state = "partial"
        elif historic_cells:
            state = "stale"
        else:
            state = "uncollected"
        if not complete:
            if historic_cells - fresh_cells:
                reasons.append("expired_run")
            if not completed_at:
                reasons.append("no_completed_run")
            if result["queue"]["requested"]:
                reasons.append("active_composition_request")
            if clipped:
                reasons.append("run_history_limit")
        else:
            reasons = []  # Older failures do not invalidate later complete evidence.
        result["sources"].append({"source": source, "origin_code": origin_code, "state": state, "complete": complete,
            "last_complete_at": _iso(completed_at), "covered_intervals": _intervals(covered_days),
            "missing_intervals": _intervals(days - covered_days),
            "supported_nights": {"min": 2, "max": 21} if source == "waavo" else None,
            "reasons": list(dict.fromkeys(reasons))})

    states = {item["state"] for item in result["sources"]}
    result["complete"] = all(item["complete"] for item in result["sources"])
    if result["complete"]:
        result["state"] = "fresh"
    elif states == {"unsupported"}:
        result["state"] = "unsupported"
    elif global_reasons or "unsupported" in states:
        result["state"] = "partial"
    elif "fresh" in states and not states & {"running", "queued"}:
        result["state"] = "partial"
    else:
        result["state"] = next((state for state in ("running", "queued", "partial", "stale", "uncollected") if state in states), "partial")
    timestamps = [item["last_complete_at"] for item in result["sources"] if item["last_complete_at"]]
    result["last_complete_at"] = max(timestamps) if timestamps else None
    result["reasons"] = list(dict.fromkeys(reason for item in result["sources"] for reason in item["reasons"]))
    return result
