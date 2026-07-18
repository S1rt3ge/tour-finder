"""Backfill the rich hotel deep-link onto offers crawled before the
deep-link builder existed. Chunked + committed per batch so it survives
the free-tier pooler dropping the connection and interleaves with a
running collector. Safe to re-run: only touches rows still on the bare
link. New offers already get the rich link at crawl time.

    $env:DATABASE_URL = "postgresql://...:5432/postgres"   # session port
    .venv\\Scripts\\python scripts\\backfill_links.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError, DBAPIError  # noqa: E402

from tourfinder import db  # noqa: E402

CHUNK = 1000
UPDATE = text("""
UPDATE offers SET link =
  'https://joinup.lv/lv/hotel/' || source_hotel_id
  || '?origin=' || origin_id || '&date=' || date_start
  || '&stay=' || nights || '&pax_adl=' || pax_adl
  || CASE WHEN children_ages <> '' THEN
       '&pax_chd=' || (length(children_ages) - length(replace(children_ages, ',', '')) + 1)
       || '&children_ages=' || children_ages ELSE '' END
  || CASE WHEN board_code <> '' THEN '&board=' || board_code ELSE '' END
WHERE id IN (
  SELECT id FROM offers WHERE link NOT LIKE '%?origin=%' LIMIT :chunk
)
""")


def main():
    engine = db.get_engine()
    total = 0
    while True:
        for attempt in range(6):
            try:
                with engine.begin() as c:
                    n = c.execute(UPDATE, {"chunk": CHUNK}).rowcount
                break
            except (OperationalError, DBAPIError):
                if attempt == 5:
                    raise
                time.sleep(5 * (attempt + 1))
        if not n:
            break
        total += n
        print(f"backfilled {total}", flush=True)
    print(f"done: {total} offers")


if __name__ == "__main__":
    main()
