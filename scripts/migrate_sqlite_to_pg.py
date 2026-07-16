"""One-shot copy of the local SQLite data into the Postgres database at
DATABASE_URL. Batches are committed as they go and retried with a fresh
connection on transient network drops, so a flaky just-provisioned
instance can't leave the target half-broken: rerun with --wipe to restart
clean, or without it to refuse when data already exists.

Usage (PowerShell):
    $env:DATABASE_URL = "postgresql://..."
    .venv\\Scripts\\python scripts\\migrate_sqlite_to_pg.py [--wipe]
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DBAPIError, OperationalError  # noqa: E402

from tourfinder import db  # noqa: E402

SQLITE_PATH = Path("data/tourfinder.db")
TABLES = ["hotels", "offers", "price_snapshots", "fetch_runs",
          "subscriptions", "alerts", "hotel_reviews"]
BATCH = 500
RETRIES = 6


def run_with_retry(engine, fn, what: str):
    """fn(conn) with commit; on connection drop, back off and reconnect."""
    for attempt in range(1, RETRIES + 1):
        try:
            with engine.connect() as conn:
                result = fn(conn)
                conn.commit()
                return result
        except (OperationalError, DBAPIError) as exc:
            if attempt == RETRIES:
                raise
            wait = 5 * attempt
            print(f"  {what}: connection dropped ({type(exc).__name__}), "
                  f"retry {attempt}/{RETRIES} in {wait}s")
            time.sleep(wait)


def main():
    if not os.environ.get("DATABASE_URL", "").startswith("postgres"):
        sys.exit("DATABASE_URL must point at Postgres")
    if not SQLITE_PATH.exists():
        sys.exit(f"no local db at {SQLITE_PATH}")
    wipe = "--wipe" in sys.argv

    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row
    engine = db.get_engine()  # creates schema on the target

    already = run_with_retry(
        engine, lambda c: c.execute(text("SELECT count(*) FROM offers")).scalar(),
        "count")
    if already and not wipe:
        sys.exit(f"target already has {already} offers — rerun with --wipe")
    if already or wipe:
        run_with_retry(
            engine,
            lambda c: c.execute(text("TRUNCATE " + ", ".join(TABLES) +
                                     " RESTART IDENTITY")),
            "wipe")
        print("target wiped")

    for table in TABLES:
        rows = [dict(r) for r in src.execute(f"SELECT * FROM {table}")]
        if not rows:
            print(f"{table}: empty")
            continue
        cols = list(rows[0].keys())
        stmt = text(f"INSERT INTO {table} ({', '.join(cols)}) "
                    f"VALUES ({', '.join(':' + c for c in cols)}) "
                    f"ON CONFLICT DO NOTHING")
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            run_with_retry(engine, lambda c: c.execute(stmt, chunk),
                           f"{table} rows {i}..{i + len(chunk)}")
        print(f"{table}: {len(rows)} rows")

    # We inserted explicit ids; bump the identity sequences past them.
    def bump(conn):
        for table in ["offers", "price_snapshots", "fetch_runs",
                      "subscriptions", "alerts", "hotel_reviews"]:
            conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT max(id) FROM {table}), 0) + 1, false)"))
    run_with_retry(engine, bump, "sequences")
    print("done")


if __name__ == "__main__":
    main()
