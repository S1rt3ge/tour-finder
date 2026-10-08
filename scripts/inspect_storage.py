"""Bounded read-only PostgreSQL storage inspection; aggregate output only.

Each report uses its own READ ONLY transaction so a timed-out optional report
does not hide the other measurements. No application startup, DDL, or pruning.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os

import psycopg
from psycopg.rows import dict_row


HORIZONS = (7, 14, 30, 60, 90)

DATABASE_SQL = """SELECT current_setting('server_version_num')::integer AS server_version_num,
    current_setting('transaction_read_only') AS transaction_read_only,
    pg_database_size(current_database()) AS current_database_bytes,
    (SELECT sum(pg_database_size(datname)) FROM pg_database) AS cluster_database_bytes"""

TABLES_SQL = """SELECT relname AS table_name,
    pg_relation_size(relid) AS heap_bytes,
    pg_table_size(relid) AS table_including_toast_bytes,
    pg_indexes_size(relid) AS index_bytes,
    pg_total_relation_size(relid) AS total_bytes,
    n_live_tup AS estimated_live_rows, n_dead_tup AS estimated_dead_rows,
    last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
    FROM pg_stat_user_tables WHERE schemaname='public'
    ORDER BY pg_total_relation_size(relid) DESC,relname"""

INDEXES_SQL = """SELECT s.relname AS table_name, s.indexrelname AS index_name,
    pg_relation_size(s.indexrelid) AS bytes,
    s.idx_scan AS scans_since_stats_reset, i.indisvalid AS valid, i.indisready AS ready
    FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid=s.indexrelid
    WHERE s.schemaname='public'
    ORDER BY pg_relation_size(s.indexrelid) DESC,s.indexrelname"""

OFFERS_SQL = """SELECT count(*) AS total,
    count(*) FILTER (WHERE date_start < %(today)s) AS departed,
    count(*) FILTER (WHERE date_start >= %(today)s) AS departing_today_or_later,
    count(*) FILTER (WHERE date_start IS NULL) AS missing_departure,
    min(date_start) AS earliest_departure, max(date_start) AS latest_departure
    FROM public.offers"""


def snapshot_sql() -> str:
    # One aggregate pass over snapshots, with one small row per offer. No
    # identifiers leave this query. The resulting counts are exact at this
    # transaction's snapshot; byte savings remain only rough estimates.
    old_counts = ",\n".join(
        f"count(*) FILTER (WHERE fetched_at < %(cutoff_{days})s) AS old_{days}"
        for days in HORIZONS)
    totals = ",\n".join(
        f"coalesce(sum(old_{days}),0) AS older_{days}, "
        f"coalesce(sum(greatest(old_{days} - CASE WHEN old_{days}=total THEN 2 ELSE 1 END,0)),0) AS eligible_{days}"
        for days in HORIZONS)
    return f"""WITH per_offer AS MATERIALIZED (
        SELECT offer_id,count(*) AS total,
            min(fetched_at) AS first_at,max(fetched_at) AS last_at,
            count(*) FILTER (WHERE fetched_at IS NULL) AS missing_timestamp,
            {old_counts}
        FROM public.price_snapshots GROUP BY offer_id
    ) SELECT coalesce(sum(total),0) AS total, count(*) AS offers_with_snapshots,
        coalesce(sum(missing_timestamp),0) AS missing_timestamp,
        min(first_at) AS oldest_snapshot, max(last_at) AS newest_snapshot,
        {totals} FROM per_offer"""


def retention_report(row: dict, snapshot_relation_bytes: int | None = None) -> dict:
    total = int(row["total"])
    missing = int(row["missing_timestamp"])
    older = {days: int(row[f"older_{days}"]) for days in HORIZONS}
    # Exclusive, disjoint age buckets; malformed/null timestamps are not
    # classified as safe to archive by this estimator.
    buckets = {"0_to_7_days": total - missing - older[7]}
    for younger, older_limit in zip(HORIZONS, HORIZONS[1:]):
        buckets[f"{younger}_to_{older_limit}_days"] = older[younger] - older[older_limit]
    buckets["over_90_days"] = older[90]
    policies = []
    for days in HORIZONS:
        eligible = int(row[f"eligible_{days}"])
        policy = {"keep_recent_days": days, "older_rows": older[days],
                  "archive_eligible_rows": eligible, "retained_rows": total - eligible}
        if snapshot_relation_bytes is not None:
            policy["proportional_relation_bytes_estimate"] = (
                round(snapshot_relation_bytes * eligible / total) if total else 0)
        policies.append(policy)
    return {
        "total": total, "offers_with_snapshots": int(row["offers_with_snapshots"]),
        "oldest_snapshot": row["oldest_snapshot"], "newest_snapshot": row["newest_snapshot"],
        "missing_timestamp": missing, "age_buckets": buckets, "retention_estimates": policies,
        "retention_policy": "keep all recent rows, latest row per offer, and one prior anchor; keep two latest rows when all history is old",
        "estimate_limits": "counts only; references and archive verification still require a deletion plan. Proportional bytes include indexes/bloat and are not immediately reclaimable disk space",
    }


def read_report(conn, sql, params=None, *, many=False):
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL lock_timeout = '3s'")
        conn.execute("SET LOCAL statement_timeout = '45s'")
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        result = conn.execute(sql, params or {})
        return result.fetchall() if many else result.fetchone()


def emit(report, data):
    def scalar(value):
        if isinstance(value, datetime):
            return value.isoformat()
        # PostgreSQL SUM(bigint) returns Decimal. These outputs are integers.
        return int(value)
    print(json.dumps({"report": report, "data": data}, default=scalar, sort_keys=True), flush=True)


def run(conn, now=None) -> bool:
    now = now or datetime.now(timezone.utc)
    params = {f"cutoff_{days}": (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ") for days in HORIZONS}
    params["today"] = now.date().isoformat()
    failed = []
    measurements = {}
    reports = (
        ("database", DATABASE_SQL, False), ("tables", TABLES_SQL, True),
        ("indexes", INDEXES_SQL, True), ("snapshots", snapshot_sql(), False),
        ("offers", OFFERS_SQL, False),
    )
    emit("inspection", {"as_of_utc": now.isoformat(), "read_only": True,
        "consistency": "each report uses its own repeatable-read snapshot; collection can continue between reports"})
    for name, sql, many in reports:
        try:
            data = read_report(conn, sql, params, many=many)
            if name == "snapshots":
                size = next((int(t["total_bytes"]) for t in measurements.get("tables", [])
                             if t["table_name"] == "price_snapshots"), None)
                data = retention_report(data, size)
            measurements[name] = data
            emit(name, data)
        except Exception as exc:
            failed.append(name)
            # Raw SQL/driver diagnostics may contain credentials. Type only.
            emit(name, {"error_type": type(exc).__name__, "complete": False})
    emit("completion", {"complete": not failed, "failed_reports": failed,
        "notes": "table row/dead-tuple statistics are estimates; database bytes include physical storage and indexes, not just live row payloads"})
    return not failed


def main() -> int:
    try:
        url = os.environ.get("DATABASE_URL", "").strip()
        if url.startswith("postgresql+psycopg://"):
            url = "postgresql://" + url[len("postgresql+psycopg://"):]
        if not url.startswith(("postgresql://", "postgres://")):
            emit("configuration", {"error": "postgresql_database_url_required"})
            return 1
        # No startup options: SET LOCAL in each transaction works through the
        # configured transaction pooler without depending on session affinity.
        with psycopg.connect(url, autocommit=True, prepare_threshold=None,
                             connect_timeout=15, row_factory=dict_row) as conn:
            return 0 if run(conn) else 1
    except Exception as exc:
        emit("connection", {"error_type": type(exc).__name__, "complete": False})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
