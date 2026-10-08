"""Standalone maintenance safety tests; fake SQL only, never a database."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts" / "maintain_storage.py"
SPEC = importlib.util.spec_from_file_location("storage_maintenance_script", PATH)
maintenance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = maintenance
SPEC.loader.exec_module(maintenance)
MIB = maintenance.MIB


def index(columns):
    return dict(index_name=maintenance.NEW_INDEX if len(columns) == 3 else maintenance.OLD_INDEX,
        bytes=30*MIB if len(columns) == 3 else 183*MIB, correct_table=True, valid=True,
        ready=True, live=True, plain=True, method="btree", unique_index=False,
        primary_index=False, replica_identity=False, constraint_index=False,
        columns=columns, key_count=len(columns), total_columns=len(columns),
        options=[0]*len(columns), collations=[0,100,0][:len(columns)],
        opclasses=[1,2,1][:len(columns)], default_opclasses=True, default_collations=True)


def report():
    return dict(database=dict(database_name="postgres", server_version_num=170004,
        database_bytes=768*MIB, cluster_bytes=783*MIB, can_monitor=True),
        tables={name: dict(table_name=name, relkind="r", table_bytes=table*MIB,
            index_bytes=(total-table)*MIB, total_bytes=total*MIB,
            estimated_live_rows=100, estimated_dead_rows=300, can_maintain=True)
            for name, table, total in [("offers",160,226),("price_snapshots",260,526)]},
        indexes={maintenance.OLD_INDEX: index(["offer_id","fetched_at"])},
        counts={"offers":100,"price_snapshots":1000},
        activity=dict(active_clients=0,idle_transactions=0,old_transactions=0,hidden_sessions=0),
        locks=dict(target_locks=0,writer_locks=0), progress=dict(index_builds=0,table_rewrites=0),
        wal=dict(wal_bytes=64*MIB), slots=dict(slots=0,retained_wal_bytes=0,inactive_retaining_slots=0))


class Result:
    def __init__(self, rows=()): self.rows = list(rows)
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


class Connection:
    def __init__(self, data=None):
        self.data = deepcopy(data or report())
        self.commands, self.transactions = [], []
        self.active = False
        self.lock_ms, self.statement_ms = 0, 0
        self.readonly = "off"
        self.acquired = True
        self.invalid_build = False
        self.wal_denied = False
        self.settings_mismatch = False
        self.failure = None
        self.count_change = False
    def __enter__(self): return self
    def __exit__(self, *args): pass
    @contextmanager
    def transaction(self):
        assert not self.active
        self.active = True
        start = len(self.commands)
        try:
            yield
        finally:
            self.transactions.append(self.commands[start:])
            self.active = False
    def execute(self, sql, params=None):
        self.commands.append(sql)
        if sql.startswith("SET "):
            if sql == "SET lock_timeout='3s'": self.lock_ms=3000
            matched = re.fullmatch("SET statement_timeout='([0-9]+)s'", sql)
            if matched: self.statement_ms=int(matched[1])*1000
            return Result()
        if sql.startswith("SHOW default_transaction_read_only"):
            return Result([{"default_transaction_read_only":self.readonly}])
        if "pg_try_advisory_lock" in sql:
            assert params == maintenance.ADVISORY_LOCK
            return Result([{"acquired":self.acquired}])
        if "pg_advisory_unlock" in sql: return Result([{"released":True}])
        if sql.startswith("SELECT current_setting('lock_timeout')"):
            return Result([{"lock_timeout":"3s","statement_timeout":"10min"}])
        if "FROM pg_settings" in sql:
            value=self.statement_ms if "'statement_timeout'" in sql else self.lock_ms
            return Result([{"milliseconds":0 if self.settings_mismatch else value}])
        mapping = {maintenance.DATABASE_SQL:"database", maintenance.TABLES_SQL:"tables",
            maintenance.INDEXES_SQL:"indexes", maintenance.ACTIVITY_SQL:"activity",
            maintenance.LOCKS_SQL:"locks", maintenance.PROGRESS_SQL:"progress",
            maintenance.WAL_SQL:"wal", maintenance.SLOTS_SQL:"slots"}
        if sql in mapping or sql.startswith("SELECT count(*) AS rows FROM public."):
            assert self.active, "measurement must be in READ ONLY transaction"
            if sql == maintenance.WAL_SQL and self.wal_denied:
                raise maintenance.psycopg.errors.InsufficientPrivilege("private fixture SQL")
            if sql.startswith("SELECT count(*) AS rows FROM public."):
                table=sql.rsplit(".",1)[1]
                return Result([{"rows":self.data["counts"][table]}])
            value=deepcopy(self.data[mapping[sql]])
            return Result(value.values() if sql in (maintenance.TABLES_SQL,maintenance.INDEXES_SQL) else [value])
        assert not self.active, "VACUUM/CONCURRENTLY cannot run inside transaction"
        assert self.lock_ms==3000 and self.statement_ms in (600000,900000)
        if self.failure: raise self.failure
        if sql.startswith("CREATE INDEX CONCURRENTLY"):
            assert sql == "CREATE INDEX CONCURRENTLY idx_snapshots_latest ON public.price_snapshots(offer_id,fetched_at,id)"
            new=index(["offer_id","fetched_at","id"])
            new["valid"]=not self.invalid_build
            self.data["indexes"][maintenance.NEW_INDEX]=new
            self.data["database"]["database_bytes"] += new["bytes"]
        elif sql.startswith("DROP INDEX CONCURRENTLY"):
            assert sql == "DROP INDEX CONCURRENTLY public.idx_snapshots_offer RESTRICT"
            old=self.data["indexes"].pop(maintenance.OLD_INDEX)
            self.data["database"]["database_bytes"] -= old["bytes"]
        elif sql.startswith("VACUUM (FULL, ANALYZE) public."):
            table=sql.rsplit(".",1)[1]
            assert table in maintenance.TABLES
            self.data["database"]["database_bytes"] -= 100*MIB
            if self.count_change: self.data["counts"][table] += 1
        else: raise AssertionError(sql)
        return Result()


def apply(conn, action="replace-index", **kwargs):
    maintenance.apply(conn, action, free_mib=kwargs.pop("free_mib",1800),
        observed_at=kwargs.pop("observed_at",datetime.now(timezone.utc).isoformat()),
        verified_prune=kwargs.pop("verified_prune",True), **kwargs)


def mutations(conn):
    return [sql for sql in conn.commands if sql.startswith(("CREATE","DROP","VACUUM"))]


def test_preflight_is_only_bounded_readonly_measurements():
    conn=Connection()
    result=maintenance.measure(conn)
    assert result["counts"] == {"offers":100,"price_snapshots":1000}
    assert not mutations(conn)
    assert conn.transactions
    for commands in conn.transactions:
        assert commands[:4] == ["SET TRANSACTION READ ONLY", "SET LOCAL lock_timeout='3s'",
            "SET LOCAL statement_timeout='45s'", "SET LOCAL max_parallel_workers_per_gather=0"]
    assert maintenance.blockers(result)==[]


def test_project_target_is_fixed_and_only_port_changes(monkeypatch):
    monkeypatch.setenv("DATABASE_URL",f"postgresql+psycopg://{maintenance.USERNAME}:fixture%40password@{maintenance.POOLER_HOST}:6543/postgres?sslmode=require")
    params=maintenance.connection_parameters()
    assert params["host"]==maintenance.POOLER_HOST and params["port"]==5432
    assert params["password"]=="fixture@password" and params["autocommit"]
    assert params["prepare_threshold"] is None and "options" not in params


@pytest.mark.parametrize("change",[
    {"host":"other.pooler.supabase.com"}, {"user":"postgres.otherproject"},
    {"database":"other"}, {"port":"1234"}, {"query":"sslmode=disable"},
    {"query":"options=-c%20statement_timeout=0"}, {"query":"sslmode=require&host=evil"},
    {"query":"sslmode=require&sslmode=disable"},
])
def test_wrong_target_or_connection_override_never_connects(monkeypatch,change,capsys):
    values=dict(host=maintenance.POOLER_HOST,user=maintenance.USERNAME,database="postgres",port="6543",query="sslmode=require")|change
    url="postgresql://{user}:do-not-print-this@{host}:{port}/{database}?{query}".format(**values)
    monkeypatch.setenv("DATABASE_URL",url)
    monkeypatch.setattr(maintenance.psycopg,"connect",lambda **_: pytest.fail("must not connect"))
    assert maintenance.main([])==1
    assert "do-not-print-this" not in capsys.readouterr().out


def test_capacity_is_reported_per_action_without_row_proportion_guess():
    data=report()
    assert maintenance.required_headroom(data,"vacuum-offers")==580*MIB
    assert maintenance.required_headroom(data,"vacuum-snapshots")==1180*MIB
    assert maintenance.required_headroom(data,"replace-index")==677*MIB
    data["indexes"][maintenance.NEW_INDEX]=index(["offer_id","fetched_at","id"])
    assert maintenance.required_headroom(data,"replace-index")==32*MIB


@pytest.mark.parametrize("free,age,code",[(0,0,"input_required"),(1,0,"insufficient_headroom"),(1800,601,"measurement_stale"),(1800,-1,"measurement_stale")])
def test_missing_insufficient_or_stale_headroom_blocks_before_mutation(free,age,code):
    conn=Connection()
    with pytest.raises(maintenance.MaintenanceError,match=code):
        apply(conn,free_mib=free,observed_at=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat())
    assert not mutations(conn)


@pytest.mark.parametrize("section,field,value",[
    ("activity","active_clients",1),("activity","idle_transactions",1),
    ("activity","old_transactions",1),("activity","hidden_sessions",1),
    ("locks","target_locks",1),("progress","index_builds",1),("progress","table_rewrites",1),
    ("database","can_monitor",False),("slots","retained_wal_bytes",65*MIB),
    ("slots","inactive_retaining_slots",1),
])
def test_concurrent_work_unknown_visibility_and_wal_retention_block_apply(section,field,value):
    data=report();data[section][field]=value
    conn=Connection(data)
    with pytest.raises(maintenance.MaintenanceError): apply(conn)
    assert not mutations(conn)


def test_denied_wal_report_stays_unknown_and_apply_fails_closed(capsys):
    conn=Connection();conn.wal_denied=True
    measured=maintenance.measure(conn)
    assert measured["wal"] is None
    assert "maintenance_wal_measurement_unknown" in maintenance.blockers(measured)
    with pytest.raises(maintenance.MaintenanceError,match="wal_measurement_unknown"): apply(conn)
    assert not mutations(conn) and "private fixture SQL" not in capsys.readouterr().out


def test_replace_index_validates_then_drops_only_old_index():
    conn=Connection();apply(conn)
    assert mutations(conn)==[
        "CREATE INDEX CONCURRENTLY idx_snapshots_latest ON public.price_snapshots(offer_id,fetched_at,id)",
        "DROP INDEX CONCURRENTLY public.idx_snapshots_offer RESTRICT"]
    create=next(i for i,s in enumerate(conn.commands) if s.startswith("CREATE"))
    drop=next(i for i,s in enumerate(conn.commands) if s.startswith("DROP"))
    assert maintenance.INDEXES_SQL in conn.commands[create+1:drop]
    assert maintenance.OLD_INDEX not in conn.data["indexes"]


def test_failed_or_invalid_concurrent_build_preserves_original_index():
    conn=Connection();conn.invalid_build=True
    with pytest.raises(maintenance.MaintenanceError,match="new_index_invalid"): apply(conn)
    assert maintenance.OLD_INDEX in conn.data["indexes"]
    assert not any(sql.startswith("DROP") for sql in conn.commands)


@pytest.mark.parametrize("name,field,value",[
    (maintenance.OLD_INDEX,"unique_index",True),(maintenance.OLD_INDEX,"replica_identity",True),
    (maintenance.OLD_INDEX,"constraint_index",True),(maintenance.OLD_INDEX,"default_collations",False),
    (maintenance.NEW_INDEX,"valid",False),(maintenance.NEW_INDEX,"ready",False),
    (maintenance.NEW_INDEX,"correct_table",False),(maintenance.NEW_INDEX,"plain",False),
    (maintenance.NEW_INDEX,"columns",["offer_id","id","fetched_at"]),
    (maintenance.NEW_INDEX,"total_columns",4),(maintenance.NEW_INDEX,"options",[0,1,0]),
])
def test_unexpected_index_definition_never_changes_either_index(name,field,value):
    data=report();data["indexes"][maintenance.NEW_INDEX]=index(["offer_id","fetched_at","id"])
    data["indexes"][name][field]=value
    conn=Connection(data)
    with pytest.raises(maintenance.MaintenanceError): apply(conn)
    assert not mutations(conn)


def test_existing_new_index_and_old_prefix_opclass_mismatch_blocks_drop():
    data=report();data["indexes"][maintenance.NEW_INDEX]=index(["offer_id","fetched_at","id"])
    data["indexes"][maintenance.NEW_INDEX]["opclasses"]=[1,9,1]
    conn=Connection(data)
    with pytest.raises(maintenance.MaintenanceError,match="prefix_mismatch"): apply(conn)
    assert not mutations(conn)


@pytest.mark.parametrize("action,table",[("vacuum-offers","offers"),("vacuum-snapshots","price_snapshots")])
def test_full_vacuum_is_one_allowlisted_command_outside_transaction(action,table):
    conn=Connection();apply(conn,action)
    assert mutations(conn)==[f"VACUUM (FULL, ANALYZE) public.{table}"]
    assert conn.statement_ms==600000 and conn.lock_ms==3000


def test_timeout_settings_are_verified_before_mutation():
    conn=Connection();conn.settings_mismatch=True
    with pytest.raises(maintenance.MaintenanceError,match="timeout_mismatch"): apply(conn)
    assert not mutations(conn)


def test_row_count_change_after_rewrite_is_explicit_failure():
    conn=Connection();conn.count_change=True
    with pytest.raises(maintenance.MaintenanceError,match="rows_changed"): apply(conn,"vacuum-offers")


@pytest.mark.parametrize("attribute,value,code",[("readonly","on","database_readonly"),("acquired",False,"already_running")])
def test_readonly_database_and_second_maintenance_fail_without_mutation(attribute,value,code):
    conn=Connection();setattr(conn,attribute,value)
    with pytest.raises(maintenance.MaintenanceError,match=code): apply(conn)
    assert not mutations(conn)


def test_lock_failure_is_not_retried_and_does_not_drop_old_index():
    conn=Connection();conn.failure=maintenance.psycopg.errors.LockNotAvailable("private secret SQL")
    with pytest.raises(maintenance.psycopg.errors.LockNotAvailable): apply(conn)
    assert len(mutations(conn))==1 and maintenance.OLD_INDEX in conn.data["indexes"]


def test_main_requires_manual_verified_prune_before_network(monkeypatch,capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME","schedule")
    monkeypatch.setattr(maintenance.psycopg,"connect",lambda **_: pytest.fail("must not connect"))
    assert maintenance.main(["--action","vacuum-offers","--verified-prune"])==1
    monkeypatch.setenv("GITHUB_EVENT_NAME","workflow_dispatch")
    assert maintenance.main(["--action","replace-index"])==1
    assert "manual_verified_prune_required" in capsys.readouterr().out


def test_main_does_not_log_raw_driver_errors(monkeypatch,capsys):
    monkeypatch.setattr(maintenance,"connection_parameters",lambda: {})
    def fail(**kwargs): raise maintenance.psycopg.OperationalError("postgresql://private:secret@privatehost")
    monkeypatch.setattr(maintenance.psycopg,"connect",fail)
    assert maintenance.main([])==1
    output=capsys.readouterr().out
    assert "private" not in output and "secret" not in output and "postgresql://" not in output
    assert json.loads(output)["code"]=="maintenance_database_operation_failed"
