"""Standalone preparation tests never connect to PostgreSQL or a default DB."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_telegram.py"
spec = importlib.util.spec_from_file_location("prepare_telegram_script", SCRIPT)
prepare = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = prepare
spec.loader.exec_module(prepare)


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, *, failure=None):
        self.sql = []
        self.transactions = []
        self.in_transaction = False
        self.failure = failure

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @contextmanager
    def transaction(self):
        assert not self.in_transaction
        self.in_transaction = True
        try:
            yield
        except Exception:
            self.transactions.append("rollback")
            raise
        else:
            self.transactions.append("commit")
        finally:
            self.in_transaction = False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if self.failure and self.failure in sql:
            raise RuntimeError("postgresql://user:secret@private.host/production")
        if "CONCURRENTLY" in sql:
            assert not self.in_transaction
        if "current_setting('statement_timeout')" in sql:
            return Cursor({"statement_timeout": "20min", "lock_timeout": "3s"})
        return Cursor({"count": 0})


@pytest.fixture(autouse=True)
def forbid_live_connection(monkeypatch):
    monkeypatch.setattr(prepare.psycopg, "connect", Mock(side_effect=AssertionError("live DB forbidden")))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://fixture:secret@example.invalid/db")


def existing_schema():
    # Start with just the old deployment's base schema.
    columns = {table: {} for table in prepare.BASE_TABLES}
    for table, names in prepare.BASE_COLUMNS.items():
        for name, kind in names.items():
            columns[table][name] = {"data_type": kind, "is_nullable": "NO", "column_default": None}
    return {"server_version_num": 170006, "database_bytes": 800000000,
            "columns": columns, "primary_keys": {}, "indexes": {}}


def ready_schema(*, snapshot_index=False):
    schema = existing_schema()
    for table, columns in (prepare.NEW_TABLES | prepare.ADDITIONS).items():
        for name, spec in (prepare.NEW_TABLES.get(table, {}) | columns).items():
            schema["columns"].setdefault(table, {})[name] = {
                "data_type": spec.type.lower(),
                "is_nullable": "NO" if spec.required or spec.primary else "YES",
                "column_default": spec.default,
            }
        if table in prepare.NEW_TABLES:
            schema["primary_keys"][table] = [n for n, c in prepare.NEW_TABLES[table].items() if c.primary]
    indexes = prepare.SMALL_INDEXES | (prepare.LARGE_INDEX if snapshot_index else {})
    for name, (table, columns) in indexes.items():
        schema["indexes"][name] = {
            "name": name, "table_name": table, "columns": columns, "valid": True,
            "ready": True, "unique": False, "no_predicate": True, "no_expressions": True, "method": "btree",
        }
    return schema


def test_preflight_is_explicitly_readonly_and_reports_missing_schema(monkeypatch):
    conn = Connection()
    def inspect(connection):
        assert connection.in_transaction
        assert connection.sql[0] == "SET TRANSACTION READ ONLY"
        return existing_schema()
    monkeypatch.setattr(prepare, "inspect_schema", inspect)
    report = prepare.preflight(conn)
    assert not report["ready"] and not report["blockers"]
    assert "table:telegram_users" in report["pending"]
    assert "column:subscriptions.owner_id" in report["pending"]
    assert not any(s.startswith(("CREATE", "ALTER", "UPDATE", "DELETE", "INSERT", "DROP")) for s in conn.sql)


def test_apply_additive_transaction_has_bounded_locks_and_postcheck(monkeypatch):
    conn = Connection()
    monkeypatch.setattr(prepare, "inspect_schema", Mock(side_effect=[existing_schema(), ready_schema()]))
    assert prepare.apply_additions(conn)["ready"]
    assert conn.sql[:3] == ["SET TRANSACTION READ WRITE", "SET LOCAL lock_timeout = '3s'",
                            "SET LOCAL statement_timeout = '60s'"]
    assert conn.transactions == ["commit"]
    assert all("IF NOT EXISTS" in sql for sql in conn.sql if sql.startswith(("CREATE", "ALTER")))
    assert not any(sql.startswith(("DELETE", "UPDATE", "INSERT", "DROP")) for sql in conn.sql)
    assert not any("idx_snapshots_latest" in sql for sql in conn.sql)


def test_apply_rolls_back_on_ddl_error(monkeypatch):
    conn = Connection(failure='ALTER TABLE public."subscriptions"')
    monkeypatch.setattr(prepare, "inspect_schema", Mock(return_value=existing_schema()))
    with pytest.raises(RuntimeError):
        prepare.apply_additions(conn)
    assert conn.transactions == ["rollback"]


def test_apply_rejects_incompatible_existing_schema_before_ddl(monkeypatch):
    schema = ready_schema()
    schema["columns"]["subscriptions"]["owner_id"]["data_type"] = "integer"
    conn = Connection()
    monkeypatch.setattr(prepare, "inspect_schema", Mock(return_value=schema))
    with pytest.raises(prepare.PreparationError, match="incompatible_schema"):
        prepare.apply_additions(conn)
    assert not any(sql.startswith(("CREATE", "ALTER")) for sql in conn.sql)


@pytest.mark.parametrize("change", ["wrong_key", "wrong_default", "nullable", "missing_required"])
def test_existing_telegram_table_shape_is_validated(change):
    schema = ready_schema()
    if change == "wrong_key":
        schema["primary_keys"]["telegram_users"] = ["chat_id"]
    elif change == "wrong_default":
        schema["columns"]["telegram_users"]["can_notify"]["column_default"] = "1"
    elif change == "nullable":
        schema["columns"]["telegram_users"]["chat_id"]["is_nullable"] = "YES"
    else:
        del schema["columns"]["telegram_users"]["chat_id"]
    assert prepare.schema_report(schema)["blockers"]


def test_postcheck_failure_rolls_back(monkeypatch):
    conn = Connection()
    monkeypatch.setattr(prepare, "inspect_schema", Mock(return_value=existing_schema()))
    with pytest.raises(prepare.PreparationError, match="postcheck_failed"):
        prepare.apply_additions(conn)
    assert conn.transactions == ["rollback"]


def test_concurrent_index_is_opt_in_autocommit_and_verified(monkeypatch):
    conn = Connection()
    connect = Mock(return_value=conn)
    monkeypatch.setattr(prepare, "connect", connect)
    monkeypatch.setattr(prepare, "inspect_schema", Mock(side_effect=[ready_schema(), ready_schema(snapshot_index=True)]))
    assert prepare.build_snapshot_index("fixture") == "ready"
    connect.assert_called_once_with("fixture", index=True)
    assert conn.transactions == []
    assert sum("CREATE INDEX CONCURRENTLY" in sql for sql in conn.sql) == 1


@pytest.mark.parametrize("field,value", [
    ("valid", False), ("ready", False), ("table_name", "offers"),
    ("columns", ["id"]), ("no_predicate", False), ("unique", True),
])
def test_existing_invalid_or_wrong_index_is_not_hidden_by_if_not_exists(monkeypatch, field, value):
    schema = ready_schema(snapshot_index=True)
    schema["indexes"]["idx_snapshots_latest"][field] = value
    conn = Connection()
    monkeypatch.setattr(prepare, "connect", Mock(return_value=conn))
    monkeypatch.setattr(prepare, "inspect_schema", Mock(return_value=schema))
    with pytest.raises(prepare.PreparationError, match="requires_manual_review"):
        prepare.build_snapshot_index("fixture")
    assert not any(sql.startswith(("CREATE", "DROP")) for sql in conn.sql)


def test_index_stops_if_pooler_ignores_startup_timeouts(monkeypatch):
    conn = Connection()
    conn.execute = Mock(return_value=Cursor({"statement_timeout": "0", "lock_timeout": "0"}))
    monkeypatch.setattr(prepare, "connect", Mock(return_value=conn))
    with pytest.raises(prepare.PreparationError, match="timeouts_not_honored"):
        prepare.build_snapshot_index("fixture")
    assert conn.execute.call_count == 1


def test_preflight_with_build_index_never_connects(monkeypatch, capsys):
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(prepare, "connect", connect)
    assert prepare.main(["--mode", "preflight", "--build-index"]) == 1
    connect.assert_not_called()
    assert "build_index_requires_apply" in capsys.readouterr().out


def test_default_cli_preflight_never_applies_or_counts(monkeypatch, capsys):
    conn = Connection()
    monkeypatch.setattr(prepare, "connect", Mock(return_value=conn))
    monkeypatch.setattr(prepare, "inspect_schema", Mock(return_value=existing_schema()))
    apply = Mock(side_effect=AssertionError("preflight cannot apply"))
    counts = Mock(side_effect=AssertionError("preflight cannot scan tables"))
    monkeypatch.setattr(prepare, "apply_additions", apply)
    monkeypatch.setattr(prepare, "telegram_counts", counts)
    assert prepare.main([]) == 0
    apply.assert_not_called()
    counts.assert_not_called()
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "preflight" and not report["ready"]


def test_connection_failure_does_not_print_credentials(monkeypatch, capsys):
    monkeypatch.setattr(prepare, "connect", Mock(side_effect=RuntimeError("postgresql://fixture:secret@private.host/db")))
    assert prepare.main([]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output and "private.host" not in output and "postgresql" not in output
    assert json.loads(output)["error"] == "RuntimeError"


def test_all_existing_ready_is_idempotent_schema_report():
    schema = ready_schema()
    # PostgreSQL formats literal defaults with casts.
    schema["columns"]["subscriptions"]["notify_mode"]["column_default"] = "'budget'::text"
    schema["columns"]["subscriptions"]["min_drop_pct"]["column_default"] = "'10'::double precision"
    schema["columns"]["subscriptions"]["min_review_rating"]["column_default"] = "4.0"
    assert prepare.schema_report(schema)["ready"]


def test_concurrent_index_rejects_known_transaction_pooler_before_connect(monkeypatch):
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(prepare, "connect", connect)
    with pytest.raises(prepare.PreparationError, match="direct_or_session_connection"):
        prepare.build_snapshot_index("postgresql://fixture@example.invalid:6543/db")
    connect.assert_not_called()
