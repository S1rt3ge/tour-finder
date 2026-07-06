"""SQLite storage: schema and connection helper."""
import sqlite3
from pathlib import Path

DEFAULT_DB = Path("data/tourfinder.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hotels(
    source          TEXT NOT NULL,
    source_hotel_id TEXT NOT NULL,
    name            TEXT NOT NULL,
    category        TEXT,
    country_id      TEXT,
    country_name    TEXT,
    city_name       TEXT,
    latitude        REAL,
    longitude       REAL,
    photo_url       TEXT,
    PRIMARY KEY (source, source_hotel_id)
);

-- Offer identity per SPEC.md: hotel + departure date + nights + board +
-- room type + tourist composition + departure airport (origin).
CREATE TABLE IF NOT EXISTS offers(
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    source_hotel_id TEXT NOT NULL,
    origin_id       TEXT NOT NULL,
    origin_name     TEXT,
    date_start      TEXT NOT NULL,
    date_end        TEXT,
    nights          INTEGER NOT NULL,
    board_code      TEXT NOT NULL,
    board_name      TEXT,
    room_code       TEXT NOT NULL DEFAULT '',
    room_name       TEXT,
    room_placement  TEXT NOT NULL DEFAULT '',
    pax_adl         INTEGER NOT NULL,
    pax_chd         INTEGER NOT NULL DEFAULT 0,
    children_ages   TEXT NOT NULL DEFAULT '',
    link            TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE (source, source_hotel_id, origin_id, date_start, nights,
            board_code, room_code, room_placement, pax_adl, pax_chd, children_ages)
);
CREATE INDEX IF NOT EXISTS idx_offers_date ON offers(date_start, nights);

-- Point-in-time price observations. "Hot" is a property of the snapshot,
-- not the offer (SPEC.md: catch "hot but pricier than its average").
CREATE TABLE IF NOT EXISTS price_snapshots(
    id          INTEGER PRIMARY KEY,
    offer_id    INTEGER NOT NULL REFERENCES offers(id),
    run_id      INTEGER REFERENCES fetch_runs(id),
    fetched_at  TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'EUR',
    is_hot      INTEGER NOT NULL DEFAULT 0,
    availability TEXT,
    stop_sale   TEXT,
    operator_avg_price_cents INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snapshots_offer ON price_snapshots(offer_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_run ON price_snapshots(run_id);

CREATE TABLE IF NOT EXISTS fetch_runs(
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    params        TEXT,
    requests_made INTEGER NOT NULL DEFAULT 0,
    offers_seen   INTEGER NOT NULL DEFAULT 0,
    errors        TEXT
);
"""


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
