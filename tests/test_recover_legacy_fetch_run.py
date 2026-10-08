"""The one-time legacy recovery must fail closed; no network or live DB here."""
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "recover_legacy_fetch_run.py"
spec = importlib.util.spec_from_file_location("recover_legacy_fetch_run_script", SCRIPT)
recovery = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recovery
spec.loader.exec_module(recovery)


def row():
    # Test metadata only, not a claim about the live row's exact started_at.
    return {"id": 1589, "started_at": "2026-10-08T22:11:35Z", "finished_at": None,
            "tier": "near", "pax_spec": "2+1:7", "requests_made": 0, "offers_seen": 0,
            "errors": None,
            "params": json.dumps({"origin": "3164", "dates": "2026-10-09:2026-11-07",
                                  "adults": 2, "children_ages": [7], "destinations": None,
                                  "max_pages": None, "tier": "near"})}


def github():
    run = {"id": recovery.CANCELLED_RUN_ID, "repository": {"full_name": recovery.REPOSITORY},
           "name": "collect", "path": recovery.WORKFLOW_PATH,
           "status": "completed", "conclusion": "cancelled", "run_attempt": 1,
           "head_sha": "a" * 40}
    job = {"id": 123, "run_id": recovery.CANCELLED_RUN_ID, "name": "collect",
           "status": "completed", "conclusion": "cancelled",
           "started_at": "2026-10-08T19:56:00Z", "completed_at": "2026-10-08T22:34:53Z"}
    api = Mock()
    api.run, api.jobs = run, {"jobs": [job], "total_count": 1}
    api.get.side_effect = lambda path: deepcopy(api.jobs if "/jobs?" in path else api.run)
    return api


class Cursor:
    def __init__(self, rows):
        self.rows = deepcopy(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, target=None):
        self.row = deepcopy(target if target is not None else row())
        self.sql, self.transactions = [], []
        self.in_transaction = False
        self.readonly = None
        self.cas_zero = False
        self.break_postcheck = False
        self.updated = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @contextmanager
    def transaction(self):
        assert not self.in_transaction
        before = deepcopy(self.row)
        self.in_transaction = True
        try:
            yield
        except Exception:
            self.row = before
            self.transactions.append("rollback")
            raise
        else:
            self.transactions.append("commit")
        finally:
            self.in_transaction = False

    def execute(self, sql, params=None):
        assert self.in_transaction
        self.sql.append((sql, params))
        if sql.startswith("SET TRANSACTION"):
            self.readonly = sql.endswith("READ ONLY")
        elif sql.startswith("SELECT"):
            assert params == (1589,)
            if sql.endswith("FOR UPDATE"):
                assert not self.readonly
            result = deepcopy(self.row)
            if self.break_postcheck and self.updated:
                result["offers_seen"] += 1
            return Cursor([result] if result else [])
        elif sql.startswith("UPDATE"):
            assert not self.readonly
            finished, errors, run_id, started, tier, pax, raw, old_errors, req, seen, source = params
            assert (run_id, started, tier, pax, raw, old_errors, req, seen) == tuple(
                self.row[key] for key in ("id", "started_at", "tier", "pax_spec", "params",
                                         "errors", "requests_made", "offers_seen"))
            assert source == json.loads(self.row["params"]).get("source", "joinup")
            if self.cas_zero:
                return Cursor([])
            self.row.update(finished_at=finished, errors=errors)
            self.updated = True
            return Cursor([self.row])
        return Cursor([])


def plan(conn=None, api=None):
    conn, api = conn or Connection(), api or github()
    report = recovery.preflight(conn, api)
    expected = {**report["expected"], "fingerprint": report["fingerprint"]}
    after = {**report["before"], "finished_at": "2026-10-08T23:10:00Z",
             "errors": json.dumps(recovery.row_errors(report["before"]) + [recovery.MARKER])}
    return conn, api, report, expected, after


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    monkeypatch.setattr(recovery.psycopg, "connect", Mock(side_effect=AssertionError("live DB forbidden")))
    monkeypatch.setattr(recovery.GitHub, "get", Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture:private-password@example.invalid/test")
    monkeypatch.setenv("GITHUB_TOKEN", "private-github-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", recovery.REPOSITORY)


def test_preflight_is_readonly_and_reports_exact_identity():
    conn, api, report, expected, _ = plan()
    assert report["outcome"] == "read_only"
    assert expected["pax"] == "2+1:7" and expected["source"] == "joinup"
    assert expected["started_at"] == conn.row["started_at"]
    assert len(expected["fingerprint"]) == 64
    assert conn.transactions == ["commit"]
    assert conn.sql[:3] == [("SET TRANSACTION READ ONLY", None),
                           ("SET LOCAL lock_timeout = '3s'", None),
                           ("SET LOCAL statement_timeout = '15s'", None)]
    assert not any(sql.startswith(("UPDATE", "DELETE", "INSERT", "ALTER", "CREATE")) for sql, _ in conn.sql)
    assert len(api.get.call_args_list) == 2


@pytest.mark.parametrize("field,value", [
    ("status", "in_progress"), ("conclusion", "success"), ("id", 123),
    ("repository", {"full_name": "fork/tour-finder"}),
    ("path", ".github/workflows/other.yml"), ("run_attempt", True), ("head_sha", "wrong")])
def test_run_proof_rejects_wrong_or_still_running_job(field, value):
    api = github()
    api.run[field] = value
    with pytest.raises(recovery.RecoveryError):
        recovery.preflight(Connection(), api)


@pytest.mark.parametrize("change", ["success", "running", "wrong_run", "ambiguous", "missing", "truncated", "bad_times"])
def test_collect_job_must_be_complete_unambiguous_and_cancelled(change):
    api = github()
    job = api.jobs["jobs"][0]
    if change == "success":
        job["conclusion"] = "success"
    elif change == "running":
        job["status"] = "in_progress"
    elif change == "wrong_run":
        job["run_id"] = 0
    elif change == "ambiguous":
        api.jobs["jobs"].append(deepcopy(job))
        api.jobs["total_count"] = 2
    elif change == "missing":
        job["name"] = "another_job"
    elif change == "truncated":
        api.jobs["total_count"] = 101
    else:
        job["completed_at"] = "2026-10-08T18:00:00Z"
    with pytest.raises(recovery.RecoveryError):
        recovery.preflight(Connection(), api)


@pytest.mark.parametrize("change", ["id", "finished", "before_job", "after_job", "naive_time", "owner", "bad_owner", "unknown_param", "private_param", "duplicate_param", "secret_error"])
def test_target_must_be_the_same_unowned_safe_legacy_row(change):
    target = row()
    params = json.loads(target["params"])
    if change == "id":
        target["id"] = 1590
    elif change == "finished":
        target["finished_at"] = "2026-10-08T22:15:00Z"
    elif change == "before_job":
        target["started_at"] = "2026-10-08T18:00:00Z"
    elif change == "after_job":
        target["started_at"] = "2026-10-08T23:00:00Z"
    elif change == "naive_time":
        target["started_at"] = "2026-10-08T22:11:35"
    elif change == "owner":
        params["collector_owner"] = {"GITHUB_RUN_ID": "123"}
    elif change == "bad_owner":
        params["collector_owner"] = []
    elif change == "unknown_param":
        params["telegram_token"] = "private-token"
    elif change == "private_param":
        params["origin"] = "https://user:password@private.example"
    elif change == "secret_error":
        target["errors"] = '["postgresql://user:password@private.example/db"]'
    target["params"] = (json.dumps(params) if change != "duplicate_param" else
                        '{"source":"private-token","source":"joinup"}')
    with pytest.raises(recovery.RecoveryError):
        recovery.preflight(Connection(target), github())


@pytest.mark.parametrize("owner", [None, {}])
def test_explicit_empty_legacy_owner_is_allowed(owner):
    target = row()
    target["params"] = json.dumps({**json.loads(target["params"]), "collector_owner": owner})
    assert recovery.preflight(Connection(target), github())["target_id"] == 1589


def test_apply_closes_one_run_preserving_errors_counters_and_params():
    target = row()
    target.update(errors='["partial: upstream timeout"]', requests_made=20, offers_seen=84)
    conn, api, report, expected, after = plan(Connection(target))
    actual = recovery.apply_recovery(conn, api, report, expected, after)
    assert actual == after == conn.row
    assert json.loads(actual["errors"]) == ["partial: upstream timeout", recovery.MARKER]
    assert {key for key in actual if actual[key] != target[key]} == {"finished_at", "errors"}
    assert len(api.get.call_args_list) == 4  # External cancellation proof is repeated.
    updates = [sql for sql, _ in conn.sql if sql.startswith("UPDATE")]
    assert len(updates) == 1
    for guard in ("id = %s", "started_at = %s", "tier = %s", "pax_spec = %s", "finished_at IS NULL",
                  "params IS NOT DISTINCT FROM %s", "errors IS NOT DISTINCT FROM %s",
                  "requests_made = %s", "offers_seen = %s", "->>'source'", "->'collector_owner'"):
        assert guard in updates[0]
    assert not any(sql.startswith(("DELETE", "INSERT", "ALTER", "CREATE")) for sql, _ in conn.sql)
    with pytest.raises(recovery.RecoveryError, match="target_already_finished"):
        recovery.apply_recovery(conn, api, report, expected, after)
    assert conn.row == after


@pytest.mark.parametrize("key", ["fingerprint", "started_at", "source", "tier", "pax"])
def test_apply_requires_every_exact_preflight_input(key):
    conn, api, report, expected, after = plan()
    expected[key] = "different"
    with pytest.raises(recovery.RecoveryError, match="preflight_identity_mismatch"):
        recovery.apply_recovery(conn, api, report, expected, after)
    assert not conn.updated


@pytest.mark.parametrize("field,value", [("params", '{"source":"waavo"}'), ("tier", "mid"),
    ("pax_spec", "2"), ("started_at", "2026-10-08T22:12:00Z"), ("offers_seen", 1),
    ("errors", '["partial"]')])
def test_changed_row_is_not_overwritten(field, value):
    conn, api, report, expected, after = plan()
    conn.row[field] = value
    before = deepcopy(conn.row)
    with pytest.raises(recovery.RecoveryError, match="target_changed_since_preflight"):
        recovery.apply_recovery(conn, api, report, expected, after)
    assert conn.row == before and conn.transactions[-1] == "rollback" and not conn.updated


def test_apply_rechecks_current_github_attempt():
    conn, api, report, expected, after = plan()
    api.run["run_attempt"] = 2
    with pytest.raises(recovery.RecoveryError, match="github_proof_changed"):
        recovery.apply_recovery(conn, api, report, expected, after)
    assert not conn.updated


@pytest.mark.parametrize("failure", ["cas_zero", "break_postcheck"])
def test_cas_or_postcheck_failure_rolls_back(failure):
    conn, api, report, expected, after = plan()
    setattr(conn, failure, True)
    with pytest.raises(recovery.RecoveryError):
        recovery.apply_recovery(conn, api, report, expected, after)
    assert conn.row == report["before"] and conn.transactions[-1] == "rollback"


def test_recovered_row_never_counts_as_successful_freshness():
    from tourfinder.cli import _run_history
    conn, api, report, expected, after = plan()
    recovery.apply_recovery(conn, api, report, expected, after)
    history_conn = Mock()
    history_conn.execute.return_value.fetchall.return_value = [after]
    record = _run_history(history_conn)[("joinup", "near", "2+1:7")]
    assert record["attempted"] is not None and record["succeeded"] is None


def test_main_preflight_audit_is_durable_without_credentials(tmp_path, monkeypatch, capsys):
    conn, api = Connection(), github()
    monkeypatch.setattr(recovery, "GitHub", lambda token: api)
    monkeypatch.setattr(recovery.psycopg, "connect", Mock(return_value=conn))
    output = tmp_path / "audit.json"
    private = tmp_path / "private.json"
    assert recovery.main(["--audit", str(output), "--private-audit", str(private)]) == 0
    audit = json.loads(output.read_text())
    assert json.loads(private.read_text())["before"] == row() and audit["outcome"] == "read_only"
    assert "params" not in audit["before"] and "errors" not in audit["before"]
    combined = output.read_text() + capsys.readouterr().out
    assert "private-password" not in combined and "private-github-token" not in combined
    kwargs = recovery.psycopg.connect.call_args.kwargs
    assert kwargs["connect_timeout"] == 15 and kwargs["prepare_threshold"] is None


def test_main_apply_persists_intent_before_change_and_final_audit(tmp_path, monkeypatch):
    conn, api, report, expected, after = plan()
    monkeypatch.setattr(recovery, "GitHub", lambda token: api)
    monkeypatch.setattr(recovery, "utcnow", lambda: after["finished_at"])
    monkeypatch.setattr(recovery.psycopg, "connect", Mock(return_value=conn))
    output = tmp_path / "audit.json"
    private = tmp_path / "private.json"
    original_apply = recovery.apply_recovery
    def apply(*args):
        saved = json.loads(private.read_text())
        assert saved["outcome"] == "intent_recorded" and saved["intended_after"] == after
        return original_apply(*args)
    monkeypatch.setattr(recovery, "apply_recovery", apply)
    argv = ["--mode", "apply", "--audit", str(output), "--private-audit", str(private)]
    for key, value in expected.items():
        argv += ["--expected-" + key.replace("_", "-"), value]
    assert recovery.main(argv) == 0
    saved = json.loads(private.read_text())
    assert saved["outcome"] == "committed" and saved["after"] == after and saved["before"] == row()
    public = json.loads(output.read_text())
    assert public["outcome"] == "committed" and public["after"]["error_count"] == 1
    assert "params" not in public["after"] and "errors" not in public["after"]


def test_public_audit_never_contains_raw_config_or_error_text():
    target = row()
    target["errors"] = '["arbitrary-upstream-diagnostic"]'
    _, _, report, _, after = plan(Connection(target))
    report.update(after=after, intended_after=after)
    result = recovery.canonical(recovery.public_audit(report))
    assert "arbitrary-upstream-diagnostic" not in result
    assert "3164" not in result and "children_ages" not in result and recovery.MARKER not in result
    assert json.loads(result)["before"]["error_count"] == 1


def test_public_and_private_audits_cannot_share_a_path(tmp_path, capsys):
    path = str(tmp_path / "same.json")
    assert recovery.main(["--audit", path, "--private-audit", path]) == 1
    recovery.psycopg.connect.assert_not_called()
    assert "private_and_public_audit_paths_must_differ" in capsys.readouterr().out


def test_main_apply_missing_inputs_does_not_even_connect(capsys):
    assert recovery.main(["--mode", "apply"]) == 1
    recovery.psycopg.connect.assert_not_called()
    assert "apply_requires_preflight_identity" in capsys.readouterr().out


def test_main_never_logs_driver_credentials(monkeypatch, capsys):
    monkeypatch.setattr(recovery.psycopg, "connect", Mock(side_effect=RuntimeError(
        "postgresql://fixture:private-password@private.example/db private-github-token")))
    assert recovery.main([]) == 1
    output = capsys.readouterr().out
    assert "RuntimeError" in output and "private-password" not in output and "private.example" not in output


def test_github_redirect_is_refused_before_forwarding_auth():
    with pytest.raises(recovery.RecoveryError, match="github_redirect_refused"):
        recovery.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.example")
