"""Manual, bounded maintenance of two fixed public tour tables.

Default preflight is read-only. Apply needs a completed verified prune and a
fresh Dashboard disk-free measurement. No application imports, user queries,
row deletion, global settings, automatic retries, or backend termination.
Session pooling is required: VACUUM/CONCURRENTLY cannot use SET LOCAL inside a
transaction, and transaction-pooler session SET does not provide affinity.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg
from psycopg.rows import dict_row

MIB = 1024 * 1024
PROJECT_REF = "oxbaqssbgtdczxbycofd"
POOLER_HOST = "aws-0-eu-west-1.pooler.supabase.com"
DATABASE = "postgres"
USERNAME = "postgres." + PROJECT_REF
ACTIONS = ("preflight", "replace-index", "vacuum-offers", "vacuum-snapshots")
TABLES = ("offers", "price_snapshots")
OLD_INDEX = "idx_snapshots_offer"
NEW_INDEX = "idx_snapshots_latest"
ADVISORY_LOCK = (1948057650, 1095910216)
HEADROOM_MAX_AGE_SECONDS = 600

DATABASE_SQL = """SELECT current_database() AS database_name,
    current_setting('server_version_num')::integer AS server_version_num,
    pg_database_size(current_database()) AS database_bytes,
    (SELECT sum(pg_database_size(datname)) FROM pg_database) AS cluster_bytes,
    (SELECT rolsuper FROM pg_roles WHERE rolname=current_user)
      OR pg_has_role(current_user,'pg_read_all_stats','USAGE') AS can_monitor"""

TABLES_SQL = """SELECT c.relname AS table_name,c.relkind,
    pg_table_size(c.oid) AS table_bytes,pg_indexes_size(c.oid) AS index_bytes,
    pg_total_relation_size(c.oid) AS total_bytes,
    s.n_live_tup AS estimated_live_rows,s.n_dead_tup AS estimated_dead_rows,
    has_table_privilege(c.oid,'MAINTAIN') AS can_maintain
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid
    WHERE n.nspname='public' AND c.relname IN ('offers','price_snapshots')"""

INDEXES_SQL = """SELECT c.relname AS index_name,pg_relation_size(c.oid) AS bytes,
    i.indrelid=to_regclass('public.price_snapshots') AS correct_table,
    i.indisvalid AS valid,i.indisready AS ready,i.indislive AS live,
    i.indisunique AS unique_index,i.indisprimary AS primary_index,
    i.indisreplident AS replica_identity,
    EXISTS(SELECT 1 FROM pg_constraint x WHERE x.conindid=c.oid) AS constraint_index,
    i.indpred IS NULL AND i.indexprs IS NULL AS plain,am.amname AS method,
    i.indnkeyatts AS key_count,i.indnatts AS total_columns,
    i.indoption::smallint[] AS options,i.indcollation::oid[] AS collations,
    i.indclass::oid[] AS opclasses,
    ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum,pos)
      JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum ORDER BY k.pos) AS columns,
    NOT EXISTS(SELECT 1 FROM unnest(i.indclass) AS x(oid)
      JOIN pg_opclass op ON op.oid=x.oid WHERE NOT op.opcdefault) AS default_opclasses,
    NOT EXISTS(SELECT 1 FROM unnest(i.indkey::smallint[],i.indcollation::oid[]) AS x(attnum,collation_oid)
      JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=x.attnum
      WHERE a.attcollation<>x.collation_oid) AS default_collations
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_index i ON i.indexrelid=c.oid LEFT JOIN pg_am am ON am.oid=c.relam
    WHERE n.nspname='public' AND c.relname IN ('idx_snapshots_offer','idx_snapshots_latest')"""

ACTIVITY_SQL = """SELECT
    count(*) FILTER (WHERE state='active') AS active_clients,
    count(*) FILTER (WHERE state LIKE 'idle in transaction%') AS idle_transactions,
    count(*) FILTER (WHERE xact_start<clock_timestamp()-interval '30 seconds') AS old_transactions,
    count(*) FILTER (WHERE state IS NULL) AS hidden_sessions
    FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
      AND backend_type='client backend'"""

LOCKS_SQL = """SELECT count(*) AS target_locks,
    count(*) FILTER (WHERE mode IN ('RowExclusiveLock','ShareUpdateExclusiveLock',
      'ShareRowExclusiveLock','ExclusiveLock','AccessExclusiveLock')) AS writer_locks
    FROM pg_locks WHERE pid IS DISTINCT FROM pg_backend_pid()
      AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
      AND relation IN (SELECT oid FROM pg_class WHERE oid IN
        (to_regclass('public.offers'),to_regclass('public.price_snapshots'))
        UNION SELECT indexrelid FROM pg_index WHERE indrelid IN
        (to_regclass('public.offers'),to_regclass('public.price_snapshots')))"""

PROGRESS_SQL = """SELECT
    (SELECT count(*) FROM pg_stat_progress_create_index WHERE datid=(SELECT oid FROM pg_database WHERE datname=current_database())) AS index_builds,
    (SELECT count(*) FROM pg_stat_progress_cluster WHERE datid=(SELECT oid FROM pg_database WHERE datname=current_database())) AS table_rewrites"""

WAL_SQL = "SELECT coalesce(sum(size),0) AS wal_bytes FROM pg_ls_waldir()"
SLOTS_SQL = """SELECT count(*) AS slots,
    coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn)),0) AS retained_wal_bytes,
    count(*) FILTER (WHERE NOT active AND restart_lsn IS NOT NULL) AS inactive_retaining_slots
    FROM pg_replication_slots WHERE database=current_database() OR database IS NULL"""


class MaintenanceError(RuntimeError):
    """Fixed non-sensitive machine code only."""


def emit(stage, **data):
    print(json.dumps({"stage": stage, **data}, sort_keys=True, default=int), flush=True)


def connection_parameters():
    raw = os.environ.get("DATABASE_URL", "").strip().replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        parsed = urlsplit(raw)
        options = parse_qs(parsed.query, strict_parsing=True)
        if (parsed.scheme not in {"postgres", "postgresql"} or parsed.hostname != POOLER_HOST
                or parsed.port not in {5432, 6543} or unquote(parsed.username or "") != USERNAME
                or parsed.path != "/" + DATABASE or not parsed.password or parsed.fragment
                or set(options) - {"sslmode", "pgbouncer", "channel_binding"}
                or any(len(values) != 1 for values in options.values())):
            raise ValueError()
        sslmode = options.get("sslmode", ["require"])[0]
        channel_binding = options.get("channel_binding", ["prefer"])[0]
        if sslmode not in {"require", "verify-ca", "verify-full"} or channel_binding not in {"prefer", "require"}:
            raise ValueError()
        # Host, username, database and port are immutable for this project.
        # Only the existing credential crosses into the session connection.
        return dict(host=POOLER_HOST, port=5432, dbname=DATABASE, user=USERNAME,
                    password=unquote(parsed.password), sslmode=sslmode,
                    channel_binding=channel_binding, autocommit=True, prepare_threshold=None,
                    connect_timeout=15, row_factory=dict_row,
                    application_name="tourfinder_storage_maintenance")
    except (ValueError, TypeError):
        raise MaintenanceError("maintenance_project_target_invalid") from None


def readonly(conn, sql, *, many=False):
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL lock_timeout='3s'")
        conn.execute("SET LOCAL statement_timeout='45s'")
        conn.execute("SET LOCAL max_parallel_workers_per_gather=0")
        rows = conn.execute(sql)
        return rows.fetchall() if many else rows.fetchone()


def measure(conn):
    report = {"measured_at": datetime.now(timezone.utc).isoformat()}
    report["database"] = dict(readonly(conn, DATABASE_SQL))
    tables = readonly(conn, TABLES_SQL, many=True)
    report["tables"] = {row["table_name"]: dict(row) for row in tables}
    report["indexes"] = {row["index_name"]: dict(row) for row in readonly(conn, INDEXES_SQL, many=True)}
    report["counts"] = {table: int(readonly(conn, f"SELECT count(*) AS rows FROM public.{table}")["rows"])
                        for table in TABLES}
    report["activity"] = dict(readonly(conn, ACTIVITY_SQL))
    report["locks"] = dict(readonly(conn, LOCKS_SQL))
    report["progress"] = dict(readonly(conn, PROGRESS_SQL))
    for name, sql in (("wal", WAL_SQL), ("slots", SLOTS_SQL)):
        try:
            report[name] = dict(readonly(conn, sql))
        except psycopg.Error:
            # Transaction rollback is complete; the report remains useful,
            # but this missing permission/measurement prevents every apply.
            report[name] = None
    return report


def index_ok(index, columns):
    return bool(index and index.get("correct_table") and index.get("valid") and index.get("ready")
                and index.get("live") and index.get("plain") and index.get("method") == "btree"
                and not index.get("unique_index") and not index.get("primary_index")
                and not index.get("replica_identity") and not index.get("constraint_index")
                and index.get("columns") == columns and index.get("key_count") == len(columns)
                and index.get("total_columns") == len(columns)
                and list(index.get("options") or []) == [0] * len(columns)
                and index.get("default_opclasses") and index.get("default_collations"))


def validate_indexes(report, *, require_new=False):
    old, new = report["indexes"].get(OLD_INDEX), report["indexes"].get(NEW_INDEX)
    if old is not None and not index_ok(old, ["offer_id", "fetched_at"]):
        raise MaintenanceError("maintenance_old_index_unexpected")
    if new is not None and not index_ok(new, ["offer_id", "fetched_at", "id"]):
        raise MaintenanceError("maintenance_new_index_invalid_or_unexpected")
    if require_new and new is None:
        raise MaintenanceError("maintenance_new_index_missing")
    if old and new and any(list(old[field]) != list(new[field])[:2] for field in ("collations", "opclasses")):
        raise MaintenanceError("maintenance_index_prefix_mismatch")


def required_headroom(report, action):
    if action == "replace-index":
        if index_ok(report["indexes"].get(NEW_INDEX), ["offer_id", "fetched_at", "id"]):
            return 32 * MIB
        table = int(report["tables"]["price_snapshots"]["table_bytes"])
        old = int(report["indexes"].get(OLD_INDEX, {}).get("bytes", 0))
        return 2 * max(table, math.ceil(old * 1.5)) + 128 * MIB
    table = {"vacuum-offers": "offers", "vacuum-snapshots": "price_snapshots"}[action]
    return 2 * int(report["tables"][table]["total_bytes"]) + 128 * MIB


def blockers(report):
    result = []
    database = report["database"]
    if database["database_name"] != DATABASE or not 170000 <= int(database["server_version_num"]) < 180000:
        result.append("maintenance_database_or_version_mismatch")
    if not database.get("can_monitor") or report["activity"]["hidden_sessions"]:
        result.append("maintenance_activity_visibility_unknown")
    if set(report["tables"]) != set(TABLES) or any(
            value["relkind"] != "r" or not value["can_maintain"] for value in report["tables"].values()):
        result.append("maintenance_tables_or_privileges_invalid")
    if any(report["activity"][name] for name in ("active_clients", "idle_transactions", "old_transactions")):
        result.append("maintenance_other_active_transactions")
    if report["locks"]["target_locks"] or any(report["progress"].values()):
        result.append("maintenance_target_busy")
    if report["wal"] is None or report["slots"] is None:
        result.append("maintenance_wal_measurement_unknown")
    elif report["slots"]["inactive_retaining_slots"] or int(report["slots"]["retained_wal_bytes"]) > 64 * MIB:
        result.append("maintenance_wal_retention_busy")
    try:
        validate_indexes(report)
    except MaintenanceError as error:
        result.append(str(error))
    return result


def headroom_guard(report, action, free_mib, observed_at, *, now=None):
    if type(free_mib) is not int or not 1 <= free_mib <= 1_000_000:
        raise MaintenanceError("maintenance_headroom_input_required")
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError()
        age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
        if not 0 <= age <= HEADROOM_MAX_AGE_SECONDS:
            raise ValueError()
    except (AttributeError, ValueError, TypeError):
        raise MaintenanceError("maintenance_headroom_measurement_stale") from None
    if free_mib * MIB < required_headroom(report, action):
        raise MaintenanceError("maintenance_insufficient_headroom")


def guard(report, action, free_mib, observed_at):
    reasons = blockers(report)
    if reasons:
        raise MaintenanceError(reasons[0])
    headroom_guard(report, action, free_mib, observed_at)


def report_measurement(stage, report):
    # Only fixed schema names, counts, bytes, booleans and aggregate states.
    # No raw query text, connection names, backend PIDs or database user data.
    required = ({action: {"minimum_bytes": required_headroom(report, action),
                          "minimum_mib": math.ceil(required_headroom(report, action) / MIB)}
                 for action in ACTIONS[1:]} if set(report["tables"]) == set(TABLES) else {})
    emit(stage, measurements=report, blockers=blockers(report), required_headroom=required,
         headroom_policy="FULL: 2*current total relation bytes+128MiB; build index: 2*max(table bytes,1.5*old index bytes)+128MiB; drop-only:32MiB. Conservative operational reserve, not a guaranteed peak.")


def set_timeouts(conn, seconds):
    if seconds not in {45, 600, 900}:
        raise MaintenanceError("maintenance_timeout_invalid")
    conn.execute("SET lock_timeout='3s'")
    conn.execute(f"SET statement_timeout='{seconds}s'")
    settings = conn.execute("SELECT current_setting('lock_timeout') AS lock_timeout, current_setting('statement_timeout') AS statement_timeout").fetchone()
    # SHOW/current_setting format varies between e.g. 600s and 10min.
    actual = conn.execute("SELECT setting::bigint AS milliseconds FROM pg_settings WHERE name='statement_timeout'").fetchone()
    lock = conn.execute("SELECT setting::bigint AS milliseconds FROM pg_settings WHERE name='lock_timeout'").fetchone()
    if not settings or int(actual["milliseconds"]) != seconds * 1000 or int(lock["milliseconds"]) != 3000:
        raise MaintenanceError("maintenance_session_timeout_mismatch")


def apply(conn, action, *, free_mib, observed_at, verified_prune):
    if action not in ACTIONS[1:] or not verified_prune:
        raise MaintenanceError("maintenance_verified_prune_required")
    original_readonly = conn.execute("SHOW default_transaction_read_only").fetchone()["default_transaction_read_only"]
    if original_readonly != "off":
        raise MaintenanceError("maintenance_database_readonly")
    acquired = conn.execute("SELECT pg_try_advisory_lock(%s,%s) AS acquired", ADVISORY_LOCK).fetchone()["acquired"]
    if not acquired:
        raise MaintenanceError("maintenance_already_running")
    try:
        before = measure(conn)
        report_measurement("pre_apply", before)
        guard(before, action, free_mib, observed_at)
        if action == "replace-index":
            new = before["indexes"].get(NEW_INDEX)
            if new is None:
                set_timeouts(conn, 900)
                conn.execute("CREATE INDEX CONCURRENTLY idx_snapshots_latest ON public.price_snapshots(offer_id,fetched_at,id)")
            # A successful CREATE return or IF NOT EXISTS is not validation.
            # Re-measure after any build; its allocated bytes also reduce the
            # externally measured headroom available for the second command.
            current = measure(conn)
            validate_indexes(current, require_new=True)
            spent = max(0, int(current["database"]["database_bytes"]) - int(before["database"]["database_bytes"]))
            remaining = free_mib - math.ceil(spent / MIB)
            guard(current, action, remaining, observed_at)
            if OLD_INDEX in current["indexes"]:
                set_timeouts(conn, 600)
                conn.execute("DROP INDEX CONCURRENTLY public.idx_snapshots_offer RESTRICT")
        else:
            set_timeouts(conn, 600)
            sql = {"vacuum-offers": "VACUUM (FULL, ANALYZE) public.offers",
                   "vacuum-snapshots": "VACUUM (FULL, ANALYZE) public.price_snapshots"}[action]
            conn.execute(sql)
        after = measure(conn)
        report_measurement("post_apply", after)
        if after["counts"] != before["counts"]:
            raise MaintenanceError("maintenance_rows_changed_during_action")
        validate_indexes(after, require_new=action == "replace-index")
        if action == "replace-index":
            if OLD_INDEX in after["indexes"]:
                raise MaintenanceError("maintenance_old_index_still_present")
        emit("complete", action=action, success=True,
             reclaimed_database_bytes=int(before["database"]["database_bytes"]) - int(after["database"]["database_bytes"]),
             cluster_bytes=int(after["database"]["cluster_bytes"]),
             below_500mb=int(after["database"]["cluster_bytes"]) < 500_000_000,
             below_target_300mib=int(after["database"]["cluster_bytes"]) <= 300 * MIB)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s,%s)", ADVISORY_LOCK)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=ACTIONS, default="preflight")
    parser.add_argument("--verified-prune", action="store_true")
    parser.add_argument("--disk-free-mib", type=int)
    parser.add_argument("--disk-measured-at")
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        if args.action != "preflight" and (os.environ.get("GITHUB_EVENT_NAME") == "schedule" or not args.verified_prune):
            raise MaintenanceError("maintenance_manual_verified_prune_required")
        parameters = connection_parameters()
        with psycopg.connect(**parameters) as conn:
            set_timeouts(conn, 45)
            if args.action == "preflight":
                report_measurement("preflight", measure(conn))
                emit("complete", action="preflight", success=True, read_only=True)
            else:
                apply(conn, args.action, free_mib=args.disk_free_mib,
                      observed_at=args.disk_measured_at, verified_prune=args.verified_prune)
        return 0
    except Exception as error:
        # Never serialize psycopg error text (it can contain credentials/SQL).
        code = str(error) if isinstance(error, MaintenanceError) else "maintenance_database_operation_failed"
        fields = {"success": False, "action": args.action, "code": code,
                  "seconds": round(time.monotonic() - started, 3)}
        if isinstance(error, psycopg.Error) and re.fullmatch(r"[A-Z0-9]{5}", error.sqlstate or ""):
            fields["sqlstate"] = error.sqlstate
        emit("failed", **fields)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
