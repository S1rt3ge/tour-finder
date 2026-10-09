"""Coverage evidence on explicit temporary SQLite; no default DB or network."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from tourfinder import coverage

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
OWNER = {"GITHUB_REPOSITORY": "fixture/tours", "GITHUB_WORKFLOW": "collect",
         "GITHUB_RUN_ID": "11", "GITHUB_RUN_ATTEMPT": "1"}


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "coverage.sqlite")
    connection.row_factory = sqlite3.Row
    connection.executescript("""CREATE TABLE fetch_runs(id INTEGER PRIMARY KEY,
        started_at TEXT,finished_at TEXT,tier TEXT,pax_spec TEXT,params TEXT,errors TEXT);
        CREATE TABLE pax_requests(spec TEXT,created_at TEXT);""")
    yield connection
    connection.close()


def filters(**changes):
    return {"date_from": "2026-10-10", "date_till": "2026-10-12", "adults": 1,
            "children_ages": "", "nights_min": 7, "nights_max": 7, "source": "waavo", **changes}


def add_run(conn, source="waavo", *, params=None, remove=(), start=None, finish=None,
            running=False, errors=None, spec="1", raw=None):
    metadata = {"source": source, "adults": 1, "children_ages": [], "max_pages": None}
    if source == "waavo":
        metadata.update(departureAirport="RIX", dateFrom="2026-10-10", dateTo="2026-10-12",
                        durationFrom=2, durationTo=21)
    else:
        metadata.update(origin="3164", dates="2026-10-10:2026-10-12", stays=[7], destinations=None)
    metadata.update(params or {})
    for key in remove:
        metadata.pop(key, None)
    start = start or NOW - timedelta(minutes=30)
    finish = finish or NOW - timedelta(minutes=10)
    conn.execute("INSERT INTO fetch_runs(started_at,finished_at,tier,pax_spec,params,errors) VALUES(?,?,?,?,?,?)",
        (start.isoformat(), None if running else finish.isoformat(), "near", spec,
         json.dumps(metadata) if raw is None else raw, json.dumps(errors) if errors is not None else None))
    conn.commit()


def read(conn, **changes):
    return coverage.get_search_coverage(conn, filters(**changes), now=NOW)


def test_empty_inventory_never_proves_coverage_and_reads_only(conn):
    statements = []
    conn.set_trace_callback(statements.append)
    result = read(conn)
    assert result["state"] == "uncollected" and result["complete"] is False
    assert result["last_complete_at"] is None
    assert result["sources"][0]["missing_intervals"] == [{"date_from": "2026-10-10", "date_till": "2026-10-12"}]
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


def test_recorded_full_waavo_run_proves_only_selected_source(conn):
    add_run(conn)
    single = read(conn)
    assert single["state"] == "fresh" and single["complete"]
    assert single["sources"][0]["missing_intervals"] == [] and single["reasons"] == []
    both = read(conn, source=None)
    assert both["state"] == "partial" and not both["complete"]
    assert {source["source"] for source in both["sources"]} == {"joinup", "waavo"}


def test_both_sources_need_independent_complete_evidence(conn):
    add_run(conn)
    add_run(conn, "joinup")
    result = read(conn, source=None)
    assert result["state"] == "fresh" and result["complete"]


def test_joinup_legacy_unknown_stays_cannot_prove_empty_search(conn):
    add_run(conn, "joinup", remove=("stays",))
    result = read(conn, source="joinup")
    assert not result["complete"] and result["state"] == "partial"
    assert "stays_not_recorded" in result["reasons"]


@pytest.mark.parametrize("field", ["departureAirport", "dateFrom", "dateTo", "durationFrom", "durationTo", "max_pages"])
def test_waavo_missing_actual_metadata_is_never_assumed_from_defaults(conn, field):
    add_run(conn, remove=(field,))
    result = read(conn)
    assert not result["complete"] and "missing_run_metadata" in result["reasons"]


@pytest.mark.parametrize("params,errors", [({"max_pages": 1}, None), ({}, ["partial: budget"]), ({}, {}), ({}, "not-empty-list")])
def test_capped_failed_and_malformed_error_runs_do_not_prove_coverage(conn, params, errors):
    add_run(conn, params=params, errors=errors)
    result = read(conn)
    assert not result["complete"] and "incomplete_run" in result["reasons"]


@pytest.mark.parametrize("cap", [False, "", [], {}])
def test_malformed_falsy_page_caps_cannot_look_like_uncapped_success(conn, cap):
    add_run(conn, params={"max_pages": cap})
    assert not read(conn)["complete"]


def test_joinup_without_recorded_destination_scope_is_unknown(conn):
    add_run(conn, "joinup", remove=("destinations",))
    result = read(conn, source="joinup")
    assert not result["complete"] and "missing_run_metadata" in result["reasons"]


def test_later_success_supersedes_failed_history_without_poisoning_status(conn):
    add_run(conn, errors=["private diagnostic"])
    add_run(conn)
    result = read(conn)
    assert result["complete"] and result["reasons"] == []
    assert "private" not in json.dumps(result)


def test_date_interval_union_covers_gap_only_after_it_was_collected(conn):
    add_run(conn, params={"dateTo": "2026-10-10"})
    add_run(conn, params={"dateFrom": "2026-10-12"})
    result = read(conn)
    assert not result["complete"]
    assert result["sources"][0]["missing_intervals"] == [{"date_from": "2026-10-11", "date_till": "2026-10-11"}]
    add_run(conn, params={"dateFrom": "2026-10-11", "dateTo": "2026-10-11"})
    assert read(conn)["complete"]


def test_night_and_date_evidence_must_cover_cartesian_scope(conn):
    add_run(conn, "joinup", params={"dates": "2026-10-10:2026-10-10", "stays": [6]})
    add_run(conn, "joinup", params={"dates": "2026-10-11:2026-10-11", "stays": [7]})
    scope = dict(source="joinup", date_till="2026-10-11", nights_min=6, nights_max=7)
    result = read(conn, **scope)
    assert not result["complete"] and result["sources"][0]["covered_intervals"] == []
    add_run(conn, "joinup", params={"dates": "2026-10-10:2026-10-11", "stays": [6, 7]})
    assert read(conn, **scope)["complete"]


def test_cadence_depends_on_actual_requested_departure_not_run_tier(conn):
    add_run(conn, start=NOW-timedelta(hours=6), finish=NOW-timedelta(hours=5), params={"dateTo": "2026-10-20"})
    assert read(conn)["state"] == "stale"
    assert read(conn, date_from="2026-10-17", date_till="2026-10-20")["complete"]


@pytest.mark.parametrize("changes", [{"adults": 2}, {"children_ages": "7"}])
def test_different_party_cannot_lend_coverage(conn, changes):
    add_run(conn)
    assert read(conn, **changes)["state"] == "uncollected"


def test_param_echo_and_canonical_party_must_both_agree(conn):
    add_run(conn, params={"adults": 2})
    assert not read(conn)["complete"]
    add_run(conn, spec="1+2:8,6", params={"children_ages": [6, 8]})
    assert read(conn, children_ages="8,6")["complete"]


@pytest.mark.parametrize("params", [{"departureAirport": "VNO"}, {"destinations": ["c_9"]}, {"stars": 4}])
def test_wrong_origin_and_restricted_source_queries_never_prove_general_coverage(conn, params):
    add_run(conn, params=params)
    assert not read(conn)["complete"]


def test_local_search_filters_do_not_create_or_relax_source_coverage(conn):
    add_run(conn)
    assert read(conn, budget_max=350, stars_min=4, board_categories="AI")["complete"]


@pytest.mark.parametrize("scope,state", [({"date_from": "2026-11-24", "date_till": "2026-11-25"}, "unsupported"),
    ({"date_till": "2026-11-24"}, "partial"), ({"origin_id": "VNO"}, "unsupported"),
    ({"room_count": 2}, "unsupported")])
def test_unsupported_scope_is_explicit_even_with_active_party_request(conn, scope, state):
    conn.execute("INSERT INTO pax_requests VALUES (?,?)", ("1", NOW.isoformat()))
    result = read(conn, **scope)
    assert result["state"] == state and not result["complete"]
    assert result["queue"]["requested"]


@pytest.mark.parametrize("low,high,state", [(1, 1, "unsupported"), (22, 30, "unsupported"), (1, 7, "partial")])
def test_waavo_nights_beyond_collector_range_are_not_covered(conn, low, high, state):
    add_run(conn, params={"durationFrom": 1, "durationTo": 30})
    result = read(conn, nights_min=low, nights_max=high)
    assert result["state"] == state and not result["complete"]
    assert "nights_outside_source_range" in result["reasons"]
    assert result["sources"][0]["supported_nights"] == {"min": 2, "max": 21}


def test_recent_composition_request_is_queued_but_not_exact_filter_evidence(conn):
    conn.execute("INSERT INTO pax_requests VALUES (?,?)", ("1", NOW.isoformat()))
    result = read(conn)
    assert result["state"] == "queued" and not result["complete"]
    assert result["queue"] == {"requested": True, "requested_at": "2026-10-09T12:00:00Z"}
    assert result["sources"][0]["covered_intervals"] == []


def test_queue_uses_canonical_exact_composition_key(conn):
    conn.execute("INSERT INTO pax_requests VALUES (?,?)", ("1+2:6,8", NOW.isoformat()))
    assert read(conn, children_ages="8,6")["queue"]["requested"]
    assert not read(conn, children_ages="6,7")["queue"]["requested"]


@pytest.mark.parametrize("at", [NOW-timedelta(days=22), NOW+timedelta(seconds=1)])
def test_expired_or_future_queue_rows_are_not_active(conn, at):
    conn.execute("INSERT INTO pax_requests VALUES (?,?)", ("1", at.isoformat()))
    assert not read(conn)["queue"]["requested"]


def test_running_requires_recent_owner_marker_and_no_superseding_run(conn):
    add_run(conn, running=True, start=NOW-timedelta(minutes=5), params={"collector_owner": OWNER})
    result = read(conn)
    assert result["state"] == "running" and not result["complete"]
    assert "running_unconfirmed" in result["reasons"]
    add_run(conn, spec="2", params={"adults": 2, "collector_owner": {**OWNER, "GITHUB_RUN_ID": "12"}})
    assert read(conn)["state"] == "partial"


@pytest.mark.parametrize("minutes,owner", [(16, OWNER), (5, {}), (5, {**OWNER, "GITHUB_RUN_ID": []})])
def test_old_or_unowned_unfinished_run_never_claims_running(conn, minutes, owner):
    add_run(conn, running=True, start=NOW-timedelta(minutes=minutes), params={"collector_owner": owner})
    result = read(conn)
    assert result["state"] == "partial" and not result["complete"]


def test_future_and_naive_completion_timestamps_are_rejected(conn):
    add_run(conn, finish=NOW+timedelta(seconds=1))
    add_run(conn, finish=NOW.replace(tzinfo=None))
    assert not read(conn)["complete"]


def test_malformed_legacy_rows_do_not_crash_or_expose_raw_values(conn):
    for raw in ("[]", "null", "not-json", '{"private":"do-not-expose"}'):
        add_run(conn, raw=raw)
    result = read(conn)
    assert not result["complete"] and "do-not-expose" not in json.dumps(result)


def test_bounded_history_does_not_hide_positive_fresh_evidence(conn, monkeypatch):
    monkeypatch.setattr(coverage, "HISTORY_LIMIT", 2)
    for _ in range(3):
        add_run(conn, errors=["partial"])
    assert "run_history_limit" in read(conn)["reasons"]
    add_run(conn)
    assert read(conn)["complete"] and read(conn)["reasons"] == []


def test_actual_sql_failure_propagates_for_caller_rollback(conn):
    conn.execute("DROP TABLE fetch_runs")
    with pytest.raises(sqlite3.OperationalError):
        read(conn)
