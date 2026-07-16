"""One-shot copy of the local SQLite data into the Postgres database at
DATABASE_URL. Idempotent-ish: refuses to run if the target already has
offers, so it can't double history.

Usage (PowerShell):
    $env:DATABASE_URL = "postgresql://..."
    .venv\\Scripts\\python scripts\\migrate_sqlite_to_pg.py
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import text  # noqa: E402

from tourfinder import db  # noqa: E402

SQLITE_PATH = Path("data/tourfinder.db")
TABLES = ["hotels", "offers", "price_snapshots", "fetch_runs",
          "subscriptions", "alerts", "hotel_reviews"]
BATCH = 1000


def main():
    if not os.environ.get("DATABASE_URL", "").startswith("postgres"):
        sys.exit("DATABASE_URL must point at Postgres")
    if not SQLITE_PATH.exists():
        sys.exit(f"no local db at {SQLITE_PATH}")

    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row
    engine = db.get_engine()  # creates schema on the target

    with engine.connect() as dst:
        already = dst.execute(text("SELECT count(*) FROM offers")).scalar()
        if already:
            sys.exit(f"target already has {already} offers — refusing to migrate twice")

        for table in TABLES:
            rows = [dict(r) for r in src.execute(f"SELECT * FROM {table}")]
            if not rows:
                print(f"{table}: empty")
                continue
            cols = list(rows[0].keys())
            stmt = text(f"INSERT INTO {table} ({', '.join(cols)}) "
                        f"VALUES ({', '.join(':' + c for c in cols)})")
            for i in range(0, len(rows), BATCH):
                dst.execute(stmt, rows[i:i + BATCH])
            print(f"{table}: {len(rows)} rows")

        # We inserted explicit ids; bump the identity sequences past them.
        for table in ["offers", "price_snapshots", "fetch_runs",
                      "subscriptions", "alerts", "hotel_reviews"]:
            dst.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT max(id) FROM {table}), 0) + 1, false)"))
        dst.commit()
    print("done")


if __name__ == "__main__":
    main()
