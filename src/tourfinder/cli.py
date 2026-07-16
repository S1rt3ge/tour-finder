"""CLI: python -m tourfinder.cli <command>

  fetch   — one-off pull from Join Up, store price snapshots
  collect — scheduler entry point: run whichever fetch tiers are due
  reviews — enrich hotels with guest reviews from an external platform
  serve   — run the local web UI
  stats   — quick DB numbers
"""
import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db

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


def cmd_collect(args):
    from .fetcher import run_fetch
    from .sources.joinup import JoinUpClient

    from .fetcher import utcnow

    log = logging.getLogger("tourfinder.collect")
    conn = db.connect(args.db)
    now = datetime.now(timezone.utc)
    # started_at is stored as UTC ISO text, so string comparison == time
    # comparison; the cutoff is computed here, not in dialect-specific SQL.
    stale_cutoff = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Reap runs abandoned by a killed process: no finished_at and started
    # over 3h ago. Their partial data stays (committed per destination);
    # only the run record is closed out so it stops looking "in progress".
    reaped = conn.execute(
        """UPDATE fetch_runs
           SET finished_at = :now, errors = '["abandoned: no completion record"]'
           WHERE finished_at IS NULL AND started_at <= :cutoff""",
        {"now": utcnow(), "cutoff": stale_cutoff},
    ).rowcount
    conn.commit()
    if reaped:
        log.warning("reaped %s abandoned run(s)", reaped)

    running = conn.execute(
        """SELECT id, started_at FROM fetch_runs
           WHERE finished_at IS NULL AND started_at > :cutoff""",
        {"cutoff": stale_cutoff},
    ).fetchone()
    if running:
        log.info("run #%s still in progress (since %s), exit",
                 running["id"], running["started_at"])
        return

    pax_specs = args.pax or DEFAULT_PAX
    for name, days_from, days_till, period_h in TIERS:
        last = conn.execute(
            """SELECT started_at FROM fetch_runs
               WHERE tier = :tier AND finished_at IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            {"tier": name},
        ).fetchone()
        if last:
            last_at = datetime.fromisoformat(last["started_at"].replace("Z", "+00:00"))
            # 10 min grace so an hourly task doesn't miss its own boundary
            if now - last_at < timedelta(hours=period_h, minutes=-10):
                log.info("tier %s: fresh (last run %s), skip", name, last["started_at"])
                continue
        log.info("tier %s: due, fetching days %s..%s for pax %s",
                 name, days_from, days_till, pax_specs)
        for spec in pax_specs:
            adults, child_ages = parse_pax(spec)
            result = run_fetch(conn, JoinUpClient(delay=args.delay),
                               days_from=days_from, days_till=days_till,
                               adults=adults, children_ages=child_ages, tier=name)
            log.info("tier %s pax %s: run #%s, offers %s, requests %s, errors: %s",
                     name, spec, result["run_id"], result["offers_seen"],
                     result["requests_made"], result["errors"] or "none")

    from . import subscriptions
    new_alerts = subscriptions.evaluate_all(conn)
    if new_alerts:
        log.info("subscriptions: %s new alert(s)", new_alerts)

    from .fetcher import prune_snapshots
    pruned = prune_snapshots(conn)
    if pruned:
        log.info("pruned %s flat snapshot(s)", pruned)


def cmd_prune(args):
    from .fetcher import prune_snapshots

    conn = db.connect(args.db)
    before = conn.execute("SELECT count(*) FROM price_snapshots").scalar()
    deleted = prune_snapshots(conn)
    print(f"snapshots: {before} -> {before - deleted} (pruned {deleted})")


def cmd_assert_fresh(args):
    """Exit non-zero when the newest snapshot is older than --hours.
    Dead-man's switch for the scheduled collector."""
    import sys

    conn = db.connect(args.db)
    newest = conn.execute("SELECT max(fetched_at) FROM price_snapshots").scalar()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    if not newest or newest < cutoff:
        print(f"STALE: newest snapshot {newest!r} older than {args.hours}h cutoff {cutoff}")
        sys.exit(1)
    print(f"fresh: newest snapshot {newest} (cutoff {cutoff})")


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
    c.set_defaults(func=cmd_collect)

    pr = sub.add_parser("prune", help="collapse flat runs of price snapshots")
    pr.set_defaults(func=cmd_prune)

    af = sub.add_parser("assert-fresh", help="fail when snapshots are stale (CI watchdog)")
    af.add_argument("--hours", type=int, default=26)
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
