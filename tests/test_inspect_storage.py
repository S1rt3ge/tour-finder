"""Read-only storage inspection tests: mocks and arithmetic, no database."""
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest


PATH = Path(__file__).resolve().parents[1] / "scripts" / "inspect_storage.py"
spec = importlib.util.spec_from_file_location("inspect_storage_script", PATH)
inspect = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = inspect
spec.loader.exec_module(inspect)


def aggregate_row():
    row = {"total": 12, "offers_with_snapshots": 4, "missing_timestamp": 1,
           "oldest_snapshot": "2026-01-01T00:00:00Z", "newest_snapshot": "2026-10-08T00:00:00Z"}
    for days, older, eligible in ((7, 8, 3), (14, 6, 2), (30, 5, 1), (60, 3, 0), (90, 1, 0)):
        row[f"older_{days}"] = older
        row[f"eligible_{days}"] = eligible
    return row


def test_age_buckets_are_disjoint_and_retention_does_not_claim_disk_reclaimed():
    report = inspect.retention_report(aggregate_row(), 1200)
    assert report["age_buckets"] == {
        "0_to_7_days": 3, "7_to_14_days": 2, "14_to_30_days": 1,
        "30_to_60_days": 2, "60_to_90_days": 2, "over_90_days": 1,
    }
    assert sum(report["age_buckets"].values()) + report["missing_timestamp"] == report["total"]
    first = report["retention_estimates"][0]
    assert first["archive_eligible_rows"] == 3 and first["retained_rows"] == 9
    assert first["proportional_relation_bytes_estimate"] == 300
    assert "not immediately reclaimable" in report["estimate_limits"]


def test_empty_history_has_zero_estimates_without_division_by_zero():
    row = {key: 0 for key in aggregate_row()}
    row.update(oldest_snapshot=None, newest_snapshot=None)
    report = inspect.retention_report(row, 8192)
    assert not any(report["age_buckets"].values())
    assert all(p["archive_eligible_rows"] == p["proportional_relation_bytes_estimate"] == 0
               for p in report["retention_estimates"])


def test_missing_table_metric_omits_misleading_bytes_estimate():
    report = inspect.retention_report(aggregate_row())
    assert all("proportional_relation_bytes_estimate" not in p for p in report["retention_estimates"])


class Connection:
    def __init__(self, fail_snapshot=False):
        self.commands = []
        self.transactions = []
        self.active = False
        self.result = None
        self.fail_snapshot = fail_snapshot

    @contextmanager
    def transaction(self):
        assert not self.active
        self.active = True
        start = len(self.commands)
        try:
            yield
        except Exception:
            self.transactions.append(("rollback", self.commands[start:]))
            raise
        else:
            self.transactions.append(("commit", self.commands[start:]))
        finally:
            self.active = False

    def execute(self, sql, params=None):
        assert self.active, "no query outside read-only transaction"
        self.commands.append(sql)
        if sql.startswith("SET "):
            return self
        assert self.commands[-5:-1] == [
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
            "SET LOCAL lock_timeout = '3s'", "SET LOCAL statement_timeout = '45s'",
            "SET LOCAL max_parallel_workers_per_gather = 0",
        ]
        if "FROM public.price_snapshots" in sql:
            if self.fail_snapshot:
                raise RuntimeError("postgresql://name:secret@private.host/database")
            self.result = aggregate_row()
        elif "FROM pg_stat_user_tables" in sql:
            self.result = [{"table_name": "price_snapshots", "total_bytes": 1200}]
        elif "FROM pg_stat_user_indexes" in sql:
            self.result = []
        else:
            self.result = {"total": 10}
        return self

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


def test_every_report_is_readonly_bounded_and_aggregate_only(capsys):
    conn = Connection()
    assert inspect.run(conn, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert len(conn.transactions) == 5
    assert all(status == "commit" for status, _ in conn.transactions)
    assert all(not sql.lstrip().startswith(("DELETE", "UPDATE", "INSERT", "CREATE", "ALTER", "VACUUM"))
               for sql in conn.commands)
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[-1]["data"]["complete"] is True
    assert {r["report"] for r in reports} >= {"database", "tables", "indexes", "snapshots", "offers"}


def test_timeout_rolls_back_only_one_report_and_never_logs_connection_text(capsys):
    conn = Connection(fail_snapshot=True)
    assert not inspect.run(conn, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert [status for status, _ in conn.transactions] == ["commit", "commit", "commit", "rollback", "commit"]
    output = capsys.readouterr().out
    assert "secret" not in output and "private.host" not in output and "postgresql://" not in output
    reports = [json.loads(line) for line in output.splitlines()]
    assert reports[-1]["data"]["failed_reports"] == ["snapshots"]
    assert next(r for r in reports if r["report"] == "offers")["data"] == {"total": 10}


def test_absent_database_url_cannot_use_application_default(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    connect = Mock(side_effect=AssertionError("no DB connection"))
    monkeypatch.setattr(inspect.psycopg, "connect", connect)
    assert inspect.main() == 1
    connect.assert_not_called()
    assert "postgresql_database_url_required" in capsys.readouterr().out
