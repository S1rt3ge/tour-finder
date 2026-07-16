"""Storage layer: one code path for local SQLite and cloud Postgres.

DATABASE_URL picks the backend:
  - unset             -> sqlite:///data/tourfinder.db (local default)
  - postgresql+psycopg://... (Supabase) -> cloud

Timestamps are stored as UTC ISO-8601 TEXT ('2026-07-07T19:00:00Z') on both
backends, so string comparison == time comparison and queries stay portable.
"""
import os
from pathlib import Path

from sqlalchemy import (Column, Float, Index, Integer, MetaData, Table, Text,
                        UniqueConstraint, create_engine, text)
from sqlalchemy.pool import NullPool

DEFAULT_DB = Path("data/tourfinder.db")

metadata = MetaData()

Table(
    "hotels", metadata,
    Column("source", Text, primary_key=True),
    Column("source_hotel_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("category", Text),
    Column("country_id", Text),
    Column("country_name", Text),
    Column("city_name", Text),
    Column("latitude", Float),
    Column("longitude", Float),
    Column("photo_url", Text),
)

# Offer identity per SPEC.md: hotel + departure date + nights + board +
# room type + tourist composition + departure airport (origin).
Table(
    "offers", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", Text, nullable=False),
    Column("source_hotel_id", Text, nullable=False),
    Column("origin_id", Text, nullable=False),
    Column("origin_name", Text),
    Column("date_start", Text, nullable=False),
    Column("date_end", Text),
    Column("nights", Integer, nullable=False),
    Column("board_code", Text, nullable=False),
    Column("board_name", Text),
    Column("room_code", Text, nullable=False, server_default=""),
    Column("room_name", Text),
    Column("room_placement", Text, nullable=False, server_default=""),
    Column("pax_adl", Integer, nullable=False),
    Column("pax_chd", Integer, nullable=False, server_default="0"),
    Column("children_ages", Text, nullable=False, server_default=""),
    Column("link", Text),
    Column("first_seen_at", Text, nullable=False),
    Column("last_seen_at", Text, nullable=False),
    UniqueConstraint("source", "source_hotel_id", "origin_id", "date_start",
                     "nights", "board_code", "room_code", "room_placement",
                     "pax_adl", "pax_chd", "children_ages",
                     name="uq_offer_identity"),
    Index("idx_offers_date", "date_start", "nights"),
)

# Point-in-time price observations. "Hot" is a property of the snapshot,
# not the offer (SPEC.md: catch "hot but pricier than its average").
Table(
    "price_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("offer_id", Integer, nullable=False),
    Column("run_id", Integer),
    Column("fetched_at", Text, nullable=False),
    Column("price_cents", Integer, nullable=False),
    Column("currency", Text, nullable=False, server_default="EUR"),
    Column("is_hot", Integer, nullable=False, server_default="0"),
    Column("availability", Text),
    Column("stop_sale", Text),
    Column("operator_avg_price_cents", Integer),
    Index("idx_snapshots_offer", "offer_id", "fetched_at"),
    Index("idx_snapshots_run", "run_id"),
)

Table(
    "fetch_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text),
    Column("tier", Text),
    Column("params", Text),
    Column("requests_made", Integer, nullable=False, server_default="0"),
    Column("offers_seen", Integer, nullable=False, server_default="0"),
    Column("errors", Text),
)

# Saved search (watchlist). filters is the JSON search payload; its
# budget_max doubles as the price threshold.
Table(
    "subscriptions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False),
    Column("filters", Text, nullable=False),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("created_at", Text, nullable=False),
)

# One firing: this offer matched this subscription for this reason.
Table(
    "alerts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("subscription_id", Integer, nullable=False),
    Column("offer_id", Integer, nullable=False),
    Column("reason", Text, nullable=False),  # new_match | price_drop
    Column("price_cents", Integer, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("seen", Integer, nullable=False, server_default="0"),
    Index("idx_alerts_sub", "subscription_id", "offer_id", "created_at"),
    Index("idx_alerts_unseen", "seen", "created_at"),
)

# Guest reviews per hotel x platform (v3).
Table(
    "hotel_reviews", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", Text, nullable=False),
    Column("source_hotel_id", Text, nullable=False),
    Column("platform", Text, nullable=False),
    Column("rating", Float),
    Column("rating_scale", Float, nullable=False, server_default="5"),
    Column("reviews_count", Integer),
    Column("summary", Text),
    Column("external_id", Text),
    Column("url", Text),
    Column("matched_name", Text),
    Column("match_status", Text, nullable=False, server_default="ok"),
    Column("fetched_at", Text, nullable=False),
    UniqueConstraint("source", "source_hotel_id", "platform",
                     name="uq_review_hotel_platform"),
    Index("idx_reviews_hotel", "source", "source_hotel_id"),
)

_engines: dict[str, object] = {}


def database_url(path: str | Path | None = None) -> str:
    env = os.environ.get("DATABASE_URL")
    if env:
        # Normalize the plain scheme Supabase hands out to the psycopg3 driver.
        if env.startswith("postgresql://"):
            env = "postgresql+psycopg://" + env[len("postgresql://"):]
        return env
    p = Path(path) if path else DEFAULT_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{p.as_posix()}"


def get_engine(path: str | Path | None = None):
    url = database_url(path)
    if url not in _engines:
        if url.startswith("sqlite"):
            engine = create_engine(url)
        else:
            # Serverless-friendly: Supabase's pgbouncer does the pooling, we
            # hold no connections between requests. prepare_threshold=None
            # disables psycopg's server-side prepared statements, which break
            # behind a transaction-mode pooler.
            engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True,
                                   connect_args={"prepare_threshold": None})
        metadata.create_all(engine)
        if url.startswith("sqlite"):
            with engine.begin() as c:
                c.exec_driver_sql("PRAGMA journal_mode=WAL")
                c.exec_driver_sql("PRAGMA busy_timeout=10000")
                # pre-SQLAlchemy databases lack the tier column
                cols = [r[1] for r in
                        c.exec_driver_sql("PRAGMA table_info(fetch_runs)")]
                if "tier" not in cols:
                    c.exec_driver_sql(
                        "ALTER TABLE fetch_runs ADD COLUMN tier TEXT")
                    c.exec_driver_sql(
                        "UPDATE fetch_runs SET tier = json_extract(params, '$.tier')")
        _engines[url] = engine
    return _engines[url]


class DB:
    """Thin wrapper keeping the old sqlite3-ish call shape: execute(sql,
    params) with :name params, fetchone()/fetchall() returning dict-like
    rows, explicit commit()."""

    def __init__(self, engine):
        self.engine = engine
        self._conn = engine.connect()

    def execute(self, sql: str, params: dict | None = None):
        res = self._conn.execute(text(sql), params or {})
        return _Result(res)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name


class _Result:
    def __init__(self, res):
        self._res = res
        self.rowcount = res.rowcount

    def fetchone(self):
        row = self._res.mappings().fetchone()
        return row

    def fetchall(self):
        return self._res.mappings().fetchall()

    def scalar(self):
        return self._res.scalar()

    def __iter__(self):
        return iter(self._res.mappings())


def connect(path: str | Path | None = None) -> DB:
    return DB(get_engine(path))
