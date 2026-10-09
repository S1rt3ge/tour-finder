"""CLI: python -m tourfinder.cli <command>

  fetch   — one-off pull from Join Up, store price snapshots
  collect — scheduler entry point: run whichever fetch tiers are due
  reviews — enrich hotels with guest reviews from an external platform
  serve   — run the local web UI
  stats   — quick DB numbers
"""
import argparse
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from . import db
from .origins import normalize_origins, normalize_source_origin

# Snapshot cadence by departure proximity (SPEC: price movement lives in
# the last week — poll near departures often, far ones daily).
# (name, days_from, days_till, period_hours)
TIERS = [
    ("near", 1, 7, 4),
    ("mid", 8, 14, 12),
    ("far", 15, 45, 24),
]

# Party compositions collected by `collect`. Each is a full crawl, so keep
# the default small; add more with `collect --pax`. Price depends on the
# exact party, so a search only matches a composition we actually collected.
DEFAULT_PAX = ["2", "2+1:7", "3"]  # couple; couple + child aged 7; three adults
SOURCES = ("joinup", "waavo")


def _party_spec(adults, children_ages=None) -> str:
    """Validate persisted party data without coercing bools/floats into ages."""
    def number(value, low, high):
        if type(value) is not int and not (isinstance(value, str) and re.fullmatch(r"[0-9]{1,2}", value.strip())):
            raise ValueError("invalid party")
        value = int(value)
        if not low <= value <= high:
            raise ValueError("invalid party")
        return value

    adults = number(adults, 1, 6)
    if children_ages is None or children_ages == "":
        children_ages = []
    elif isinstance(children_ages, str):
        children_ages = children_ages.split(",")
    if not isinstance(children_ages, (list, tuple)) or len(children_ages) > 4:
        raise ValueError("invalid party")
    ages = sorted(number(age, 0, 17) for age in children_ages)
    return str(adults) + (f"+{len(ages)}:" + ",".join(map(str, ages)) if ages else "")


def _canonical_pax_spec(spec) -> str:
    if (not isinstance(spec, str) or len(spec) > 128
            or not re.fullmatch(r"[0-9]{1,2}(?:\+[0-4]:(?:[0-9]{1,2}(?: *, *[0-9]{1,2})*)?)?", spec.strip())):
        raise ValueError("invalid party")
    adults, ages = parse_pax(spec)
    return _party_spec(adults, ages)


def active_pax_specs(conn, now=None) -> list[str]:
    """All recent requests and active approved subscriptions, deduplicated.

    The task/time budget bounds each invocation. Keeping every active party in
    the fair planner prevents a newer request from displacing an older one.
    Explicit requests expire after 21 days; a live saved search keeps its party
    active until the search expires or its owner loses access.
    """
    from .telegram_bot import approved_user_ids

    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")
    specs = dict.fromkeys(DEFAULT_PAX)
    for row in conn.execute("""SELECT spec FROM pax_requests WHERE created_at >= :cutoff
            ORDER BY created_at,spec""", {"cutoff": cutoff}):
        try:
            specs[_canonical_pax_spec(row["spec"])] = None
        except (ValueError, TypeError):
            continue
    approved = approved_user_ids(conn)
    if approved:
        params = {f"owner{i}": owner for i, owner in enumerate(sorted(approved))}
        marks = ",".join(f":{name}" for name in params)
        for row in conn.execute(f"""SELECT filters FROM subscriptions
                WHERE enabled=1 AND owner_id IN ({marks}) ORDER BY id""", params):
            try:
                filters = json.loads(row["filters"])
                if not isinstance(filters, dict):
                    continue
                first, last = date.fromisoformat(filters["date_from"]), date.fromisoformat(filters["date_till"])
                if last < now.date() + timedelta(days=1) or first > last or first > now.date() + timedelta(days=45):
                    continue
                if "RIX" not in normalize_origins(filters.get("origins")).split(","):
                    continue
                specs[_party_spec(filters["adults"], filters.get("children_ages"))] = None
            except (ValueError, TypeError, KeyError):
                continue  # malformed legacy personal data never schedules work
    return list(specs)


def active_collection_scopes(conn, now=None) -> list[tuple[str, str]]:
    """Exact (airport, party) demand; never multiply unrelated user requests.

    Legacy global composition requests and defaults remain Riga-only. New
    durable requests and active approved subscriptions retain their own airport
    selection. These activate the existing three date tiers, not an exact-filter
    refresh or any extension beyond the current 45-day collection horizon.
    """
    from .collection_requests import active_scopes
    from .telegram_bot import approved_user_ids

    now = now or datetime.now(timezone.utc)
    scopes = dict.fromkeys(("RIX", spec) for spec in active_pax_specs(conn, now))
    requested = list(active_scopes(conn, now=now))
    approved = approved_user_ids(conn)
    if approved:
        params = {f"owner{i}": owner for i, owner in enumerate(sorted(approved))}
        marks = ",".join(f":{name}" for name in params)
        for row in conn.execute(f"SELECT filters FROM subscriptions WHERE enabled=1 AND owner_id IN ({marks}) ORDER BY id", params):
            try:
                requested.append(json.loads(row["filters"]))
            except (ValueError, TypeError):
                continue
    for filters in requested:
        try:
            first, last = date.fromisoformat(filters["date_from"]), date.fromisoformat(filters["date_till"])
            if first > last or last < now.date() + timedelta(days=1) or first > now.date() + timedelta(days=45):
                continue
            spec = _party_spec(filters["adults"], filters.get("children_ages"))
            for origin in normalize_origins(filters.get("origins")).split(","):
                scopes[(origin, spec)] = None
        except (ValueError, TypeError, KeyError):
            continue
    return list(scopes)


def parse_pax(spec: str) -> tuple[int, list[int]]:
    """'2' -> (2, []); '2+1:7' -> (2, [7]); '2+2:6,8' -> (2, [6, 8])."""
    spec = spec.strip()
    party, _, ages = spec.partition("+")
    adults = int(party)
    if not ages:
        return adults, []
    count, _, age_list = ages.partition(":")
    child_ages = [int(a) for a in age_list.split(",") if a.strip()] if age_list else []
    if int(count) != len(child_ages):
        raise ValueError(f"pax '{spec}': child count {count} != ages {child_ages}")
    return adults, child_ages


def cmd_fetch(args):
    from .fetcher import run_fetch
    from .sources.joinup import JoinUpClient

    conn = db.connect(args.db)
    pax_specs = args.pax or [str(args.adults)]
    for spec in pax_specs:
        adults, child_ages = parse_pax(spec)
        result = run_fetch(
            conn, JoinUpClient(delay=args.delay),
            days_from=args.days_from, days_till=args.days,
            adults=adults, children_ages=child_ages,
            only_destinations=args.destinations.split(",") if args.destinations else None,
            max_pages=args.max_pages,
        )
        print(f"pax {spec}: run #{result['run_id']}, offers stored {result['offers_seen']}, "
              f"requests {result['requests_made']}, errors: {result['errors'] or 'none'}")


@dataclass(frozen=True)
class CollectTask:
    source: str
    tier: str
    pax: str
    days_from: int
    days_till: int
    period_hours: int
    origin: str = "RIX"

    @property
    def key(self):
        return (self.source, self.tier, self.pax, self.origin)


def _run_history(conn):
    """Read the small run log once; legacy Join Up params lack source."""
    history = {}
    for row in conn.execute(
            "SELECT id, tier, pax_spec, started_at, finished_at, errors, params "
            "FROM fetch_runs ORDER BY id").fetchall():
        try:
            params = json.loads(row["params"] or "{}")
            source = params.get("source", "joinup")
            started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
            errors = json.loads(row["errors"]) if row["errors"] else []
            pax = _canonical_pax_spec(row["pax_spec"])
            if source not in SOURCES or started.utcoffset() is None:
                continue
        except (ValueError, TypeError, AttributeError):
            continue  # malformed metadata is never evidence of fresh coverage
        field = "origin" if source == "joinup" else "departureAirport"
        # Before airport-scoped collection, omitted origin meant Riga only.
        origin = "RIX" if field not in params else normalize_source_origin(source, params[field])
        if origin is None:
            continue
        key = (source, row["tier"], pax, origin)
        item = history.setdefault(key, {"attempted": None, "succeeded": None, "durations": []})
        if item["attempted"] is None or started > item["attempted"]:
            item["attempted"] = started
        if row["finished_at"] and errors == [] and not params.get("max_pages"):
            try:
                finished = datetime.fromisoformat(row["finished_at"].replace("Z", "+00:00"))
                if finished.utcoffset() is None:
                    continue
            except (ValueError, AttributeError):
                continue
            if item["succeeded"] is None or finished > item["succeeded"]:
                item["succeeded"] = finished
            if finished >= started:
                item["durations"].append((finished, (finished - started).total_seconds()))
    return history


def _duration_estimate(task, history, now):
    """Recent full fetch time plus headroom; unknown work remains admissible.

    Prefer the latest exact party. Otherwise use up to three recent successes
    for the same source/tier, whose request volume is usually comparable.
    Neither historic fetch_runs nor the current fetch timer include the
    separate subscription evaluation.
    """
    cutoff = now - timedelta(days=7)

    def recent(record):
        return [sample for sample in record.get("durations", [])
                if cutoff <= sample[0] <= now]

    exact = recent(history.get(task.key, {}))
    if exact:
        observed = max(exact, key=lambda sample: sample[0])[1]
    else:
        peers = [sample for key, record in history.items()
                 if key[:2] == task.key[:2] and key[3] == task.origin for sample in recent(record)]
        if not peers:
            return None
        latest = sorted(peers, key=lambda sample: sample[0], reverse=True)[:3]
        observed = median(sample[1] for sample in latest)
    return observed * 1.2 + 30


def _collect_tasks(pax_specs):
    scopes = dict.fromkeys(("RIX", spec) if isinstance(spec, str) else tuple(spec) for spec in pax_specs)
    return [CollectTask(source, tier, spec, start, end, hours, origin)
            for tier, start, end, hours in TIERS
            for origin, spec in scopes for source in SOURCES]


def plan_collection(conn, pax_specs, now=None, *, history=None):
    """Rotate overdue work by last attempt so failures cannot starve other tiers.

    Only full successful runs refresh that exact source/tier/composition. Once
    all new tasks have had a turn, the oldest attempted overdue task goes first.
    """
    now = now or datetime.now(timezone.utc)
    if history is None:
        history = _run_history(conn)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    due = []
    for task in _collect_tasks(pax_specs):
        record = history.get(task.key, {})
        finished = record.get("succeeded")
        if finished and now - finished < timedelta(hours=task.period_hours, minutes=-10):
            continue
        due.append(task)
    due.sort(key=lambda task: history.get(task.key, {}).get("attempted") or epoch)
    return due


def _prepare_collection(conn, now):
    """Recover abandoned rows; a live/unknown owner produces a failed job.

    GitHub's workflow concurrency prevents two collect jobs from running at
    once. A record carrying an earlier run ID for this same workflow is known
    to be abandoned even when its start is recent. Unknown/local owners retain
    the existing conservative three-hour lease.
    """
    from .fetcher import collector_owner

    owner = collector_owner()
    cutoff = now - timedelta(hours=3)
    recovered = 0
    blocking = []
    for row in conn.execute(
            "SELECT id, started_at, params FROM fetch_runs WHERE finished_at IS NULL").fetchall():
        try:
            params = json.loads(row["params"] or "{}")
            old_owner = params.get("collector_owner") or {}
            old_time = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
            if not isinstance(old_owner, dict) or old_time.utcoffset() is None:
                raise ValueError("unknown collection lease")
        except (ValueError, TypeError, AttributeError):
            blocking.append(row["id"])
            continue
        previous_job = (all(owner.get(key) and old_owner.get(key) for key in
                            ("GITHUB_RUN_ID", "GITHUB_WORKFLOW", "GITHUB_REPOSITORY"))
                        and (owner.get("GITHUB_RUN_ID"), owner.get("GITHUB_RUN_ATTEMPT", "1")) !=
                            (old_owner.get("GITHUB_RUN_ID"), old_owner.get("GITHUB_RUN_ATTEMPT", "1"))
                        and owner.get("GITHUB_WORKFLOW") == old_owner.get("GITHUB_WORKFLOW")
                        and owner.get("GITHUB_REPOSITORY") == old_owner.get("GITHUB_REPOSITORY"))
        if previous_job or old_time <= cutoff:
            conn.execute(
                "UPDATE fetch_runs SET finished_at=:now, errors=:errors "
                "WHERE id=:id AND finished_at IS NULL",
                {"now": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "id": row["id"],
                 "errors": '["abandoned: no completion record"]'})
            recovered += 1
        else:
            blocking.append(row["id"])
    conn.commit()
    if blocking:
        raise RuntimeError("collection blocked by unfinished run(s): " +
                           ", ".join(map(str, blocking)))
    return recovered


def cmd_collect(args):
    from .fetcher import run_fetch, run_waavo_fetch
    from .sources.joinup import JoinUpClient
    from .sources.waavo import WaavoClient

    log = logging.getLogger("tourfinder.collect")
    conn = db.connect(args.db)
    max_tasks, max_minutes = args.max_tasks, args.max_minutes
    if max_tasks < 1 or max_minutes <= 0:
        conn.close()
        raise ValueError("max-tasks and max-minutes must be positive")
    deadline = time.monotonic() + max_minutes * 60
    completed, attempted, failures, deferred = 0, 0, 0, 0
    try:
        recovered = _prepare_collection(conn, datetime.now(timezone.utc))
        if recovered:
            log.warning("marked %s abandoned run(s) incomplete", recovered)
        pax_specs = args.pax or active_collection_scopes(conn)
        history = _run_history(conn)
        tasks = plan_collection(conn, pax_specs, history=history)
        log.info("%s source/tier/party tasks due; invocation budget: %s tasks, %s min",
                 len(tasks), max_tasks, max_minutes)
        from . import subscriptions
        for task in tasks:
            remaining = deadline - time.monotonic()
            if attempted >= max_tasks or remaining <= 0:
                break
            estimate = _duration_estimate(task, history, datetime.now(timezone.utc))
            if completed and estimate is not None and estimate > remaining:
                # Do not create a fetch_run: keeping its old attempt time lets
                # this task move to the front of the next invocation.
                deferred += 1
                log.info("deferred %s/%s/%s/%s: estimated_fetch_seconds=%.1f remaining_seconds=%.1f",
                         *task.key, estimate, remaining)
                continue
            attempted += 1
            adults, ages = parse_pax(task.pax)
            log.info("fetching %s/%s/%s/%s days %s..%s", *task.key,
                     task.days_from, task.days_till)
            fetch, client_type = ((run_fetch, JoinUpClient) if task.source == "joinup"
                                  else (run_waavo_fetch, WaavoClient))
            fetch_started = time.monotonic()
            result = fetch(conn, client_type(delay=args.delay),
                           days_from=task.days_from, days_till=task.days_till,
                           adults=adults, children_ages=ages,
                           tier=task.tier, pax_spec=task.pax, deadline=deadline, origin=task.origin)
            elapsed = max(0.0, time.monotonic() - fetch_started)
            if result["errors"]:
                failures += 1
            else:
                completed += 1
                history.setdefault(task.key, {"durations": []})["durations"].append(
                    (datetime.now(timezone.utc), elapsed))
            log.info("%s/%s/%s/%s run #%s: %s offers, %s requests, completed=%s",
                     *task.key, result["run_id"], result["offers_seen"],
                     result["requests_made"], not result["errors"])
            # A completed task must make notifications reachable even when the
            # remaining backlog spans several scheduled invocations.
            new_alerts = subscriptions.evaluate_all(conn, deadline=deadline)
            if new_alerts:
                log.info("subscriptions: %s new alert(s)", new_alerts)
        if not attempted:
            subscriptions.evaluate_all(conn, deadline=deadline)
        pending = len(plan_collection(conn, pax_specs))
        log.info("collection summary: completed=%s attempted=%s failed_or_partial=%s pending=%s deferred=%s",
                 completed, attempted, failures, pending, deferred)
        if failures:
            raise RuntimeError(f"{failures} collection task(s) failed or incomplete; {pending} pending")
        return {"completed": completed, "attempted": attempted, "pending": pending}
    finally:
        conn.close()


def cmd_prune(args):
    from .fetcher import prune_snapshots

    conn = db.connect(args.db)
    before = conn.execute("SELECT count(*) FROM price_snapshots").scalar()
    deleted = prune_snapshots(conn)
    print(f"snapshots: {before} -> {before - deleted} (pruned {deleted})")


def cmd_assert_fresh(args):
    """All requested source/tier/party tasks need a recent successful run."""
    conn = db.connect(args.db)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        history = _run_history(conn)
        tasks = _collect_tasks(args.pax or active_collection_scopes(conn))
        stale = ["/".join(task.key) for task in tasks
                 if not history.get(task.key, {}).get("succeeded") or
                 history[task.key]["succeeded"] < cutoff]
        if stale:
            raise RuntimeError("STALE or never completed: " + ", ".join(stale))
        print(f"fresh: all {len(tasks)} source/tier/party tasks completed within {args.hours}h")
    finally:
        conn.close()


def cmd_reviews(args):
    from . import reviews as reviews_mod
    from .sources.reviews import get_provider

    conn = db.connect(args.db)
    provider = get_provider(args.provider)
    if not provider.available():
        print(f"provider '{args.provider}' has no credentials — set the API key "
              f"(GOOGLE_PLACES_API_KEY for google) and retry. Nothing fetched.")
        return
    result = reviews_mod.enrich(conn, provider, max_age_days=args.max_age_days,
                                limit=args.limit)
    print(f"reviews[{args.provider}]: candidates {result['candidates']}, "
          f"checked {result['checked']}, stored {result['stored']}, "
          f"errors {result.get('errors', 0)}")


def cmd_serve(args):
    import uvicorn
    uvicorn.run("tourfinder.webapp:app", host="127.0.0.1", port=args.port,
                reload=args.reload)


def cmd_stats(args):
    conn = db.connect(args.db)
    q = lambda sql: conn.execute(sql).scalar()
    print("backend:  ", conn.dialect)
    print("hotels:   ", q("SELECT count(*) FROM hotels"))
    print("offers:   ", q("SELECT count(*) FROM offers"))
    print("snapshots:", q("SELECT count(*) FROM price_snapshots"))
    print("hot now:  ", q("""SELECT count(DISTINCT offer_id) FROM price_snapshots
                             WHERE is_hot=1"""))
    for r in conn.execute(
            """SELECT id, started_at, finished_at, requests_made, offers_seen, errors
               FROM fetch_runs ORDER BY id DESC LIMIT 5"""):
        print(f"run #{r['id']}: {r['started_at']} -> {r['finished_at']} "
              f"req={r['requests_made']} offers={r['offers_seen']} "
              f"errors={'yes' if r['errors'] else 'no'}")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="tourfinder")
    p.add_argument("--db", default=str(db.DEFAULT_DB))
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="pull tours and store snapshots")
    f.add_argument("--days", type=int, default=30, help="window end, days from today")
    f.add_argument("--days-from", type=int, default=1, help="window start, days from today")
    f.add_argument("--adults", type=int, default=2, help="ignored if --pax given")
    f.add_argument("--pax", action="append",
                   help="party composition, repeatable: '2', '2+1:7', '2+2:6,8'")
    f.add_argument("--destinations", help="comma list of ids, e.g. c_8,c_4 (default: all)")
    f.add_argument("--max-pages", type=int, help="page cap per search (for testing)")
    f.add_argument("--delay", type=float, default=1.2, help="seconds between requests")
    f.set_defaults(func=cmd_fetch)

    c = sub.add_parser("collect", help="run due fetch tiers (scheduler entry point)")
    c.add_argument("--pax", action="append",
                   help=f"party composition, repeatable (default: {DEFAULT_PAX})")
    c.add_argument("--delay", type=float, default=1.2)
    c.add_argument("--max-tasks", type=int, default=6,
                   help="maximum source/tier/party tasks per invocation")
    c.add_argument("--max-minutes", type=float, default=50,
                   help="cooperative collection time budget; partial runs remain due")
    c.set_defaults(func=cmd_collect)

    pr = sub.add_parser("prune", help="collapse flat runs of price snapshots")
    pr.set_defaults(func=cmd_prune)

    af = sub.add_parser("assert-fresh", help="fail when snapshots are stale (CI watchdog)")
    af.add_argument("--hours", type=int, default=26)
    af.add_argument("--pax", action="append", help="party composition, repeatable")
    af.set_defaults(func=cmd_assert_fresh)

    rv = sub.add_parser("reviews", help="enrich hotels with guest reviews")
    rv.add_argument("--provider", default="google", help="review platform (default: google)")
    rv.add_argument("--limit", type=int, help="max hotels this run (spares API quota)")
    rv.add_argument("--max-age-days", type=int, default=30,
                    help="refetch reviews older than this")
    rv.set_defaults(func=cmd_reviews)

    s = sub.add_parser("serve", help="run local web UI")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    st = sub.add_parser("stats", help="DB numbers")
    st.set_defaults(func=cmd_stats)

    args = p.parse_args()
    if args.command == "collect":
        # runs headless under pythonw from Task Scheduler — log to a file
        log_path = Path(args.db).parent / "collect.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
    args.func(args)


if __name__ == "__main__":
    main()
