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
                        UniqueConstraint, create_engine, inspect, text)
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
    Column("operator", Text),  # real operator behind an aggregator source
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
    Index("idx_snapshots_latest", "offer_id", "fetched_at", "id"),
    Index("idx_snapshots_run", "run_id"),
)

Table(
    "fetch_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text),
    Column("tier", Text),
    Column("pax_spec", Text),
    Column("params", Text),
    Column("requests_made", Integer, nullable=False, server_default="0"),
    Column("offers_seen", Integer, nullable=False, server_default="0"),
    Column("errors", Text),
)

# Party compositions requested from the UI. collect crawls DEFAULT_PAX
# plus the freshest few of these (each one is a full extra crawl, so the
# picker in cli.py caps and ages them out).
Table(
    "pax_requests", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("spec", Text, nullable=False, unique=True),  # e.g. '3+1:13'
    Column("created_at", Text, nullable=False),
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
    Column("owner_id", Text),
    Column("notify_mode", Text, nullable=False, server_default="budget"),
    Column("min_drop_pct", Float, nullable=False, server_default="10"),
    Column("min_saving_cents", Integer, nullable=False, server_default="10000"),
    Column("min_review_rating", Float, nullable=False, server_default="4"),
    Column("min_review_count", Integer, nullable=False, server_default="20"),
    Column("evaluation_offset", Integer, nullable=False, server_default="0"),
    Index("idx_subscriptions_owner", "owner_id", "enabled"),
    sqlite_autoincrement=True,
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
    Column("evidence", Text, nullable=False, server_default="{}"),
    Index("idx_alerts_sub", "subscription_id", "offer_id", "created_at"),
    Index("idx_alerts_unseen", "seen", "created_at"),
    sqlite_autoincrement=True,
)

Table(
    "id_counters", metadata,
    Column("name", Text, primary_key=True),
    Column("last_id", Integer, nullable=False),
)

Table(
    "telegram_users", metadata,
    Column("user_id", Text, primary_key=True),
    Column("chat_id", Text, nullable=False),
    Column("first_name", Text, nullable=False, server_default=""),
    Column("can_notify", Integer, nullable=False, server_default="0"),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)

Table(
    "telegram_deliveries", metadata,
    Column("alert_id", Integer, primary_key=True),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("claimed_at", Text),
    Column("next_attempt_at", Text),
    Column("sent_at", Text),
    Column("message_id", Text),
    Column("last_error", Text),
    Column("owner_id", Text),
    Column("subscription_id", Integer),
    Column("source", Text),
    Column("source_hotel_id", Text),
    Index("idx_telegram_delivery_queue", "status", "next_attempt_at"),
    Index("idx_telegram_delivery_owner", "owner_id", "sent_at"),
)

Table(
    "telegram_access_requests", metadata,
    Column("user_id", Text, primary_key=True),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("first_name", Text, nullable=False, server_default=""),
    Column("requested_at", Text, nullable=False),
    Column("decided_at", Text),
    Column("decided_by", Text),
    Index("idx_telegram_access_status", "status", "requested_at"),
)

# Owner-scoped, idempotent search requests. They retain the requested filters;
# queue admission is separate from measured source coverage.
Table(
    "collection_requests", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("owner_id", Text, nullable=False),
    Column("request_key", Text, nullable=False),
    Column("filters", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("expires_at", Text, nullable=False),
    UniqueConstraint("owner_id", "request_key", name="uq_collection_request_owner_scope"),
    Index("idx_collection_request_active", "expires_at", "owner_id"),
)

Table(
    "telegram_updates", metadata,
    Column("update_id", Text, primary_key=True),
    Column("processed_at", Text, nullable=False),
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
                                   connect_args={"prepare_threshold": None, "connect_timeout": 8})
        # Production web requests only read the explicitly prepared schema.
        # Avoid DDL/inspection locks during cold starts or archive fallback.
        if url.startswith("sqlite") or not os.environ.get("VERCEL"):
            with engine.begin() as schema_conn:
                metadata.create_all(schema_conn)
                if engine.dialect.name == "postgresql":
                    # Request filters and owner IDs are private to the backend.
                    # New installations must never expose this table through
                    # Supabase's client API, even with default public grants.
                    schema_conn.exec_driver_sql("ALTER TABLE collection_requests ENABLE ROW LEVEL SECURITY")
                    schema_conn.exec_driver_sql("REVOKE ALL ON collection_requests FROM PUBLIC")
                    # These roles are Supabase-specific, so plain PostgreSQL
                    # installations without them remain supported.
                    for role in ("anon", "authenticated"):
                        if schema_conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}).first():
                            schema_conn.exec_driver_sql(f"REVOKE ALL ON collection_requests FROM {role}")
            _ensure_new_columns(engine)
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


def _ensure_new_columns(engine):
    """create_all doesn't ALTER existing tables — add columns introduced
    after the first deployment (no-op when already present)."""
    added = {
        "fetch_runs": ["pax_spec TEXT"], "offers": ["operator TEXT"],
        "subscriptions": ["owner_id TEXT", "notify_mode TEXT NOT NULL DEFAULT 'budget'",
                          "min_drop_pct FLOAT NOT NULL DEFAULT 10",
                          "min_saving_cents INTEGER NOT NULL DEFAULT 10000",
                          "min_review_rating FLOAT NOT NULL DEFAULT 4",
                          "min_review_count INTEGER NOT NULL DEFAULT 20",
                          "evaluation_offset INTEGER NOT NULL DEFAULT 0"],
        "alerts": ["evidence TEXT NOT NULL DEFAULT '{}'"],
        "telegram_deliveries": ["owner_id TEXT", "subscription_id INTEGER", "source TEXT", "source_hotel_id TEXT"],
    }
    inspector = inspect(engine)
    for table, cols in added.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        for col in cols:
            if col.split()[0] in existing:
                continue
            with engine.begin() as c:
                c.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col}")
            if table == "fetch_runs" and col.startswith("pax_spec"):
                _backfill_pax_spec(engine)


def _backfill_pax_spec(engine):
    """Derive pax_spec for pre-existing runs from their params JSON, so the
    per-(tier, pax) freshness check doesn't consider everything stale."""
    import json as _json
    with engine.begin() as c:
        rows = c.execute(text(
            "SELECT id, params FROM fetch_runs "
            "WHERE pax_spec IS NULL AND params IS NOT NULL")).fetchall()
        for row in rows:
            try:
                p = _json.loads(row[1])
                adults = int(p.get("adults") or 0)
                ages = sorted(int(a) for a in (p.get("children_ages") or []))
            except (ValueError, TypeError):
                continue
            if not adults:
                continue
            spec = str(adults)
            if ages:
                spec += f"+{len(ages)}:{','.join(str(a) for a in ages)}"
            c.execute(text("UPDATE fetch_runs SET pax_spec = :s WHERE id = :i"),
                      {"s": spec, "i": row[0]})


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

    def rollback(self):
        self._conn.rollback()

    def next_retained_id(self, table: str) -> int | None:
        """Legacy SQLite tables can reuse deleted IDs despite autoincrement=True.

        Preserve identifiers referenced by the delivery audit. Acquire SQLite's
        write lock first; PostgreSQL uses its existing sequence instead.
        """
        if self.dialect != "sqlite":
            return None
        floors = {
            "alerts": "SELECT id AS value FROM alerts UNION ALL SELECT alert_id FROM telegram_deliveries",
            "subscriptions": "SELECT id AS value FROM subscriptions UNION ALL SELECT subscription_id FROM alerts UNION ALL SELECT subscription_id FROM telegram_deliveries",
        }
        if table not in floors:
            raise ValueError("Unsupported ID table")
        return self.execute(f"""INSERT INTO id_counters(name,last_id)
            SELECT :name,coalesce(max(value),0)+1 FROM ({floors[table]}) retained WHERE 1=1
            ON CONFLICT(name) DO UPDATE SET last_id=max(id_counters.last_id+1,excluded.last_id)
            RETURNING last_id""", {"name": table}).scalar()

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
