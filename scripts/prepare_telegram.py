"""Explicit, additive PostgreSQL preparation. Never imports application startup.

Default preflight uses a READ ONLY transaction. Apply changes only the listed
Telegram structures and defaults; it never updates or deletes existing rows.
The large snapshot index requires the separate --build-index option.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row


@dataclass(frozen=True)
class Column:
    type: str
    required: bool = False
    default: str | None = None
    primary: bool = False

    def ddl(self, name: str) -> str:
        return (f'"{name}" {self.type}'
                + (" PRIMARY KEY" if self.primary else " NOT NULL" if self.required else "")
                + (f" DEFAULT {self.default}" if self.default is not None else ""))


# Identifiers below are source constants, never command-line or database input.
NEW_TABLES = {
    "id_counters": {
        "name": Column("TEXT", primary=True), "last_id": Column("INTEGER", True),
    },
    "telegram_users": {
        "user_id": Column("TEXT", primary=True), "chat_id": Column("TEXT", True),
        "first_name": Column("TEXT", True, "''"),
        "can_notify": Column("INTEGER", True, "0"),
        "created_at": Column("TEXT", True), "updated_at": Column("TEXT", True),
    },
    "telegram_deliveries": {
        "alert_id": Column("INTEGER", primary=True),
        "status": Column("TEXT", True, "'pending'"),
        "attempts": Column("INTEGER", True, "0"),
        **{name: Column("TEXT") for name in (
            "claimed_at", "next_attempt_at", "sent_at", "message_id", "last_error",
            "owner_id", "source", "source_hotel_id")},
        "subscription_id": Column("INTEGER"),
    },
    "telegram_updates": {
        "update_id": Column("TEXT", primary=True), "processed_at": Column("TEXT", True),
    },
    "telegram_access_requests": {
        "user_id": Column("TEXT", primary=True),
        "status": Column("TEXT", True, "'pending'"),
        "first_name": Column("TEXT", True, "''"),
        "requested_at": Column("TEXT", True),
        "decided_at": Column("TEXT"), "decided_by": Column("TEXT"),
    },
}
ADDITIONS = {
    "subscriptions": {
        "owner_id": Column("TEXT"), "notify_mode": Column("TEXT", True, "'budget'"),
        "min_drop_pct": Column("DOUBLE PRECISION", True, "10"),
        "min_saving_cents": Column("INTEGER", True, "10000"),
        "min_review_rating": Column("DOUBLE PRECISION", True, "4"),
        "min_review_count": Column("INTEGER", True, "20"),
        "evaluation_offset": Column("INTEGER", True, "0"),
    },
    "alerts": {"evidence": Column("TEXT", True, "'{}'")},
    "telegram_deliveries": {
        name: NEW_TABLES["telegram_deliveries"][name]
        for name in ("owner_id", "subscription_id", "source", "source_hotel_id")
    },
}
BASE_TABLES = (
    "hotels", "offers", "price_snapshots", "fetch_runs", "pax_requests",
    "subscriptions", "alerts", "hotel_reviews",
)
BASE_COLUMNS = {
    "subscriptions": {"id": "integer", "enabled": "integer"},
    "alerts": {"id": "integer", "subscription_id": "integer", "offer_id": "integer"},
    "price_snapshots": {"id": "integer", "offer_id": "integer", "fetched_at": "text"},
}
SMALL_INDEXES = {
    "idx_subscriptions_owner": ("subscriptions", ["owner_id", "enabled"]),
    "idx_telegram_delivery_queue": ("telegram_deliveries", ["status", "next_attempt_at"]),
    "idx_telegram_delivery_owner": ("telegram_deliveries", ["owner_id", "sent_at"]),
    "idx_telegram_access_status": ("telegram_access_requests", ["status", "requested_at"]),
}
LARGE_INDEX = {"idx_snapshots_latest": ("price_snapshots", ["offer_id", "fetched_at", "id"])}


class PreparationError(RuntimeError):
    """A fixed, non-sensitive error code safe for workflow logs."""


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url.startswith("postgresql+psycopg://"):
        url = "postgresql://" + url[len("postgresql+psycopg://"):]
    if not url.startswith(("postgresql://", "postgres://")):
        raise PreparationError("postgresql_database_url_required")
    return url


def connect(url: str, *, index: bool = False):
    # Index builds use a direct/session connection: transaction-pooler backend
    # switching cannot guarantee session timeouts. Ordinary DDL uses SET LOCAL.
    extra = {"options": "-c lock_timeout=3000 -c statement_timeout=1200000"} if index else {}
    return psycopg.connect(
        url, autocommit=True, prepare_threshold=None, connect_timeout=15,
        row_factory=dict_row, **extra,
    )


def transaction(conn, *, readonly: bool):
    """Caller enters conn.transaction() first, before any statement here."""
    conn.execute("SET TRANSACTION READ ONLY" if readonly else "SET TRANSACTION READ WRITE")
    conn.execute("SET LOCAL lock_timeout = '3s'")
    conn.execute("SET LOCAL statement_timeout = '60s'")


def inspect_schema(conn) -> dict[str, Any]:
    meta = conn.execute("""SELECT current_setting('server_version_num')::integer AS server_version_num,
        pg_database_size(current_database()) AS database_bytes""").fetchone()
    tables = set(BASE_TABLES) | set(NEW_TABLES)
    rows = conn.execute("""SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns WHERE table_schema='public' AND table_name=ANY(%s)
        ORDER BY table_name, ordinal_position""", (sorted(tables),)).fetchall()
    columns: dict[str, dict[str, dict]] = {}
    for row in rows:
        columns.setdefault(row["table_name"], {})[row["column_name"]] = row
    keys = conn.execute("""SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_catalog=tc.constraint_catalog AND kcu.constraint_schema=tc.constraint_schema
         AND kcu.constraint_name=tc.constraint_name AND kcu.table_name=tc.table_name
        WHERE tc.table_schema='public' AND tc.constraint_type='PRIMARY KEY' AND tc.table_name=ANY(%s)
        ORDER BY tc.table_name, kcu.ordinal_position""", (list(NEW_TABLES),)).fetchall()
    primary_keys: dict[str, list[str]] = {}
    for row in keys:
        primary_keys.setdefault(row["table_name"], []).append(row["column_name"])
    indexes = conn.execute("""SELECT ci.relname AS name, ct.relname AS table_name,
        i.indisvalid AS valid, i.indisready AS ready, i.indisunique AS unique,
        i.indpred IS NULL AS no_predicate, i.indexprs IS NULL AS no_expressions,
        am.amname AS method,
        ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
              FROM generate_series(1, i.indnatts) k) AS columns
        FROM pg_index i JOIN pg_class ci ON ci.oid=i.indexrelid
        JOIN pg_class ct ON ct.oid=i.indrelid
        JOIN pg_namespace ns ON ns.oid=ci.relnamespace
        JOIN pg_am am ON am.oid=ci.relam
        WHERE ns.nspname='public' AND ci.relname=ANY(%s)""",
        (list(SMALL_INDEXES | LARGE_INDEX),)).fetchall()
    return {**meta, "columns": columns, "primary_keys": primary_keys,
            "indexes": {row["name"]: row for row in indexes}}


def index_state(schema, name, spec) -> str:
    row = schema["indexes"].get(name)
    if row is None:
        return "missing"
    if not row["valid"] or not row["ready"]:
        return "invalid"
    table, columns = spec
    if (row["table_name"] != table or row["columns"] != columns or row["unique"]
            or row["method"] != "btree" or not row["no_predicate"] or not row["no_expressions"]):
        return "incompatible"
    return "ready"


def default_matches(actual, spec: Column) -> bool:
    value = str(actual or "").split("::", 1)[0].strip("() ")
    if spec.type in {"INTEGER", "DOUBLE PRECISION"}:
        try:
            # PostgreSQL can render numeric defaults as '10'::double precision.
            return Decimal(value.strip("'")) == Decimal(spec.default)
        except (InvalidOperation, TypeError):
            return False
    return value == spec.default


def schema_report(schema) -> dict[str, Any]:
    columns = schema["columns"]
    blockers = []
    if schema["server_version_num"] < 170000:
        blockers.append("PostgreSQL 17 or newer required")
    for table in BASE_TABLES:
        if table not in columns:
            blockers.append(f"missing base table: {table}")
    for table, expected in BASE_COLUMNS.items():
        for name, kind in expected.items():
            if columns.get(table, {}).get(name, {}).get("data_type") != kind:
                blockers.append(f"incompatible base column: {table}.{name}")
    pending = [f"table:{name}" for name in NEW_TABLES if name not in columns]
    for table, expected in (NEW_TABLES | ADDITIONS).items():
        # The two maps overlap for delivery: validate its full shape separately.
        expected = NEW_TABLES.get(table, {}) | expected
        if table not in columns:
            continue
        if table in NEW_TABLES:
            expected_key = [name for name, spec in NEW_TABLES[table].items() if spec.primary]
            if schema["primary_keys"].get(table) != expected_key:
                blockers.append(f"incompatible primary key: {table}")
        for name, spec in expected.items():
            row = columns[table].get(name)
            if row is None:
                if name in ADDITIONS.get(table, {}):
                    pending.append(f"column:{table}.{name}")
                else:
                    blockers.append(f"missing required column: {table}.{name}")
            elif (row["data_type"] != spec.type.lower()
                  or ((spec.required or spec.primary) and row["is_nullable"] != "NO")):
                blockers.append(f"incompatible column: {table}.{name}")
            elif spec.default is not None and not default_matches(row.get("column_default"), spec):
                blockers.append(f"incompatible default: {table}.{name}")
    states = {name: index_state(schema, name, spec) for name, spec in (SMALL_INDEXES | LARGE_INDEX).items()}
    for name in SMALL_INDEXES:
        if states[name] == "missing":
            pending.append(f"index:{name}")
        elif states[name] != "ready":
            blockers.append(f"{states[name]} index: {name}")
    return {
        "server_version_num": schema["server_version_num"],
        "database_bytes": schema["database_bytes"],
        "ready": not blockers and not pending,
        "pending": pending, "blockers": blockers, "indexes": states,
    }


def preflight(conn) -> dict[str, Any]:
    with conn.transaction():
        transaction(conn, readonly=True)
        return schema_report(inspect_schema(conn))


def create_index_sql(name, spec, *, concurrently=False) -> str:
    table, columns = spec
    keys = ", ".join(f'"{column}"' for column in columns)
    return (f'CREATE INDEX {"CONCURRENTLY " if concurrently else ""}IF NOT EXISTS '
            f'"{name}" ON public."{table}" ({keys})')


def apply_additions(conn) -> dict[str, Any]:
    with conn.transaction():
        transaction(conn, readonly=False)
        before = schema_report(inspect_schema(conn))
        if before["blockers"]:
            raise PreparationError("incompatible_schema")
        for table, columns in NEW_TABLES.items():
            definitions = ", ".join(spec.ddl(name) for name, spec in columns.items())
            conn.execute(f'CREATE TABLE IF NOT EXISTS public."{table}" ({definitions})')
        for table, columns in ADDITIONS.items():
            for name, spec in columns.items():
                conn.execute(f'ALTER TABLE public."{table}" ADD COLUMN IF NOT EXISTS {spec.ddl(name)}')
        for name, spec in SMALL_INDEXES.items():
            conn.execute(create_index_sql(name, spec))
        after = schema_report(inspect_schema(conn))
        if not after["ready"]:
            raise PreparationError("postcheck_failed")
        return after


def build_snapshot_index(url: str) -> str:
    # Deliberately outside every transaction: PostgreSQL forbids concurrent
    # index creation in a transaction. A failed build is reported, never dropped.
    if urlsplit(url).port == 6543:
        raise PreparationError("snapshot_index_requires_direct_or_session_connection")
    with connect(url, index=True) as conn:
        settings = conn.execute("""SELECT current_setting('statement_timeout') AS statement_timeout,
            current_setting('lock_timeout') AS lock_timeout""").fetchone()
        if settings != {"statement_timeout": "20min", "lock_timeout": "3s"}:
            raise PreparationError("index_timeouts_not_honored")
        schema = inspect_schema(conn)
        name, spec = next(iter(LARGE_INDEX.items()))
        state = index_state(schema, name, spec)
        if state in {"invalid", "incompatible"}:
            raise PreparationError("existing_snapshot_index_requires_manual_review")
        if state == "missing":
            conn.execute(create_index_sql(name, spec, concurrently=True))
        if index_state(inspect_schema(conn), name, spec) != "ready":
            raise PreparationError("snapshot_index_postcheck_failed")
    return "ready"


def telegram_counts(conn) -> dict[str, int]:
    with conn.transaction():
        transaction(conn, readonly=True)
        return {table: conn.execute(f'SELECT count(*) AS count FROM public."{table}"').fetchone()["count"]
                for table in ("telegram_users", "telegram_deliveries", "telegram_updates", "telegram_access_requests")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "apply"), default="preflight")
    parser.add_argument("--build-index", action="store_true")
    args = parser.parse_args(argv)
    stage = "configuration"
    try:
        if args.build_index and args.mode != "apply":
            raise PreparationError("build_index_requires_apply")
        url = database_url()
        with connect(url) as conn:
            stage = "preflight"
            report = preflight(conn)
            print(json.dumps({"stage": stage, **report}, sort_keys=True), flush=True)
            if args.mode == "apply":
                stage = "apply_additions"
                report = apply_additions(conn)
                print(json.dumps({"stage": stage, **report}, sort_keys=True), flush=True)
                if args.build_index:
                    stage = "build_snapshot_index"
                    print(json.dumps({"stage": stage, "status": build_snapshot_index(url)}), flush=True)
                stage = "postcheck"
                print(json.dumps({"stage": stage, "telegram_counts": telegram_counts(conn)}, sort_keys=True), flush=True)
        return 0 if not report["blockers"] else 1
    except Exception as exc:
        # DB exceptions may contain passwords/connection strings. Never print
        # their text, traceback, SQL, or diagnostics, even for failed DDL.
        error = {"ok": False, "stage": stage, "error": type(exc).__name__}
        if isinstance(exc, PreparationError):
            error["code"] = str(exc)
        print(json.dumps(error), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
