"""CLI: python -m tourfinder.cli <command>

  fetch  — pull tours from Join Up and store price snapshots
  serve  — run the local web UI
  stats  — quick DB numbers
"""
import argparse
import logging

from . import db


def cmd_fetch(args):
    from .fetcher import run_fetch
    from .sources.joinup import JoinUpClient

    conn = db.connect(args.db)
    client = JoinUpClient(delay=args.delay)
    result = run_fetch(
        conn, client,
        days=args.days, adults=args.adults,
        only_destinations=args.destinations.split(",") if args.destinations else None,
        max_pages=args.max_pages,
    )
    print(f"run #{result['run_id']}: offers stored {result['offers_seen']}, "
          f"requests {result['requests_made']}, errors: {result['errors'] or 'none'}")


def cmd_serve(args):
    import uvicorn
    uvicorn.run("tourfinder.webapp:app", host="127.0.0.1", port=args.port,
                reload=args.reload)


def cmd_stats(args):
    conn = db.connect(args.db)
    q = lambda sql: conn.execute(sql).fetchone()[0]
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
    f.add_argument("--days", type=int, default=30, help="departure window from tomorrow")
    f.add_argument("--adults", type=int, default=2)
    f.add_argument("--destinations", help="comma list of ids, e.g. c_8,c_4 (default: all)")
    f.add_argument("--max-pages", type=int, help="page cap per search (for testing)")
    f.add_argument("--delay", type=float, default=1.2, help="seconds between requests")
    f.set_defaults(func=cmd_fetch)

    s = sub.add_parser("serve", help="run local web UI")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    st = sub.add_parser("stats", help="DB numbers")
    st.set_defaults(func=cmd_stats)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
