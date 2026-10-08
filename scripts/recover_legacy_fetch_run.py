"""One-time recovery of fetch_run 1589 after cancelled Actions run 37835734029.

Preflight is read-only. Apply requires the exact preflight fingerprint and row
identity, rechecks GitHub, and only records an incomplete end to this one run.
No application imports, migrations, lease changes, deletions, or fresh coverage.
The private runner-local audit retains before/after images for a separately
reviewed reverse CAS; the uploaded summary excludes raw params/errors. Never
restore a before image blindly over subsequent changes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request

import psycopg
from psycopg.rows import dict_row


TARGET_ID = 1589
CANCELLED_RUN_ID = 37835734029
REPOSITORY = "S1rt3ge/tour-finder"
WORKFLOW_PATH = ".github/workflows/collect.yml"
MARKER = ("abandoned: confirmed cancelled GitHub run 37835734029 "
          "(legacy recovery 1589)")
COLUMNS = ("id", "started_at", "finished_at", "tier", "pax_spec", "params",
           "requests_made", "offers_seen", "errors")
SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM public.fetch_runs WHERE id = %s"
PARAM_KEYS = {"source", "collector_owner", "origin", "dates", "adults",
              "children_ages", "destinations", "max_pages", "tier", "dateFrom", "dateTo"}


class RecoveryError(Exception):
    """Only fixed, credential-free error codes may be used here."""


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result
    except (AttributeError, TypeError, ValueError):
        raise RecoveryError("invalid_timestamp") from None


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def fingerprint(row, proof):
    return hashlib.sha256(canonical({"before": row, "github": proof}).encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RecoveryError("github_redirect_refused")


class GitHub:
    def __init__(self, token):
        if not token:
            raise RecoveryError("github_token_required")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path):
        # The path is constructed from constants/validated integers below.
        request = urllib.request.Request("https://api.github.com/repos/" + REPOSITORY + path,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "tour-finder-legacy-recovery"})
        with self.opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise RecoveryError("github_http_status")
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise RecoveryError("github_response_too_large")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise RecoveryError("github_invalid_response")
        return data


def cancelled_job_proof(github):
    run = github.get(f"/actions/runs/{CANCELLED_RUN_ID}")
    attempt = run.get("run_attempt")
    if (run.get("id") != CANCELLED_RUN_ID
            or run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("path") != WORKFLOW_PATH or run.get("name") != "collect"
            or run.get("status") != "completed" or run.get("conclusion") != "cancelled"
            or type(attempt) is not int or attempt < 1
            or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))):
        raise RecoveryError("github_run_not_confirmed_cancelled")
    result = github.get(f"/actions/runs/{CANCELLED_RUN_ID}/attempts/{attempt}/jobs?per_page=100")
    jobs = result.get("jobs")
    if (not isinstance(jobs, list) or result.get("total_count") != len(jobs)
            or len(jobs) > 100):
        raise RecoveryError("github_jobs_incomplete")
    matches = [job for job in jobs if job.get("name") == "collect"]
    if len(matches) != 1:
        raise RecoveryError("github_collect_job_ambiguous")
    job = matches[0]
    if (job.get("run_id") != CANCELLED_RUN_ID or type(job.get("id")) is not int
            or job["id"] <= 0 or job.get("status") != "completed"
            or job.get("conclusion") != "cancelled"
            or timestamp(job.get("started_at")) > timestamp(job.get("completed_at"))):
        raise RecoveryError("github_job_not_confirmed_cancelled")
    return {"repository": REPOSITORY, "workflow_path": WORKFLOW_PATH,
            "run_id": CANCELLED_RUN_ID, "run_attempt": attempt,
            "head_sha": run["head_sha"], "job_id": job["id"],
            "job_started_at": job["started_at"], "job_completed_at": job["completed_at"],
            "status": "completed", "conclusion": "cancelled",
            "run_url": f"https://github.com/{REPOSITORY}/actions/runs/{CANCELLED_RUN_ID}"}


def public_params(raw):
    """Audit only known collector configuration; unknown/private fields fail closed."""
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        params = json.loads(raw or "{}", object_pairs_hook=unique_keys)
    except (ValueError, TypeError):
        raise RecoveryError("invalid_params") from None
    if not isinstance(params, dict) or set(params) - PARAM_KEYS:
        raise RecoveryError("nonpublic_or_unknown_params")
    if params.get("collector_owner") not in (None, {}):
        raise RecoveryError("collector_owner_present")
    source = params.get("source", "joinup")
    if source not in {"joinup", "waavo"}:
        raise RecoveryError("invalid_source")
    # Restrict the strings that can enter the public recovery artifact. This
    # excludes URLs, credentials and arbitrary nested data even under known keys.
    for key, value in params.items():
        if key in {"source", "collector_owner"}:
            continue
        if value is None:
            continue
        if key == "tier" and value in {"near", "mid", "far"}:
            continue
        if key in {"origin", "dates", "dateFrom", "dateTo"}:
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9:_-]{1,80}", value):
                continue
        elif key in {"adults", "max_pages"}:
            if type(value) is int and 0 <= value <= 10000:
                continue
        elif key in {"children_ages", "destinations"} and isinstance(value, list):
            if len(value) <= 200 and all(
                    (type(item) is int and 0 <= item <= 1000000)
                    or (key == "destinations" and isinstance(item, str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item)) for item in value):
                continue
        raise RecoveryError("nonpublic_or_invalid_param_value")
    return params, source


def row_errors(row):
    try:
        errors = json.loads(row["errors"]) if row["errors"] else []
    except (ValueError, TypeError):
        raise RecoveryError("invalid_existing_errors") from None
    if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors):
        raise RecoveryError("invalid_existing_errors")
    # Existing arbitrary exception text is not safe to publish in an artifact.
    if any(len(item) > 1000 or re.search(r"://|token|password|secret|authorization", item, re.I)
           for item in errors):
        raise RecoveryError("existing_errors_require_private_review")
    return errors


def validate_row(row, proof):
    if not row or row.get("id") != TARGET_ID:
        raise RecoveryError("target_not_found")
    if row.get("finished_at") is not None:
        raise RecoveryError("target_already_finished")
    _, source = public_params(row.get("params"))
    if row.get("tier") not in {"near", "mid", "far"} or not isinstance(row.get("pax_spec"), str):
        raise RecoveryError("invalid_target_identity")
    if not re.fullmatch(r"[1-9][0-9]?(?:\+[1-9][0-9]?:[0-9,]+)?", row["pax_spec"]):
        raise RecoveryError("invalid_target_pax")
    started = timestamp(row.get("started_at"))
    if not timestamp(proof["job_started_at"]) <= started <= timestamp(proof["job_completed_at"]):
        raise RecoveryError("target_outside_cancelled_job")
    if any(type(row.get(key)) is not int or row[key] < 0 for key in ("requests_made", "offers_seen")):
        raise RecoveryError("invalid_target_counters")
    row_errors(row)
    return source


def transaction_settings(conn, *, readonly):
    conn.execute("SET TRANSACTION READ ONLY" if readonly else "SET TRANSACTION READ WRITE")
    conn.execute("SET LOCAL lock_timeout = '3s'")
    conn.execute("SET LOCAL statement_timeout = '15s'")


def preflight(conn, github):
    proof = cancelled_job_proof(github)
    with conn.transaction():
        transaction_settings(conn, readonly=True)
        row = conn.execute(SELECT, (TARGET_ID,)).fetchone()
        source = validate_row(row, proof)
    row = dict(row)
    return {"mode": "preflight", "outcome": "read_only", "checked_at": utcnow(),
            "target_id": TARGET_ID, "before": row, "github": proof,
            "fingerprint": fingerprint(row, proof),
            "expected": {"started_at": row["started_at"], "source": source,
                         "tier": row["tier"], "pax": row["pax_spec"]}}


def require_expected(report, expected):
    if (not re.fullmatch(r"[0-9a-f]{64}", expected.get("fingerprint") or "")
            or expected["fingerprint"] != report["fingerprint"]
            or {key: expected.get(key) for key in report["expected"]} != report["expected"]):
        raise RecoveryError("preflight_identity_mismatch")


UPDATE = """UPDATE public.fetch_runs SET finished_at = %s, errors = %s
WHERE id = %s AND started_at = %s AND tier = %s AND pax_spec = %s
  AND finished_at IS NULL
  AND params IS NOT DISTINCT FROM %s AND errors IS NOT DISTINCT FROM %s
  AND requests_made = %s AND offers_seen = %s
  AND COALESCE(params::jsonb->>'source', 'joinup') = %s
  AND COALESCE(params::jsonb->'collector_owner', 'null'::jsonb) IN ('null'::jsonb, '{}'::jsonb)
RETURNING """ + ", ".join(COLUMNS)


def apply_recovery(conn, github, report, expected, intended_after):
    require_expected(report, expected)
    correct_after = {**report["before"], "finished_at": intended_after.get("finished_at"),
                     "errors": json.dumps(row_errors(report["before"]) + [MARKER])}
    if (intended_after != correct_after
            or timestamp(intended_after.get("finished_at")) < timestamp(report["github"]["job_completed_at"])):
        raise RecoveryError("invalid_recovery_intent")
    # Repeat the external proof immediately before acquiring the one row lock.
    proof = cancelled_job_proof(github)
    if proof != report["github"]:
        raise RecoveryError("github_proof_changed")
    with conn.transaction():
        transaction_settings(conn, readonly=False)
        row = conn.execute(SELECT + " FOR UPDATE", (TARGET_ID,)).fetchone()
        source = validate_row(row, proof)
        if fingerprint(dict(row), proof) != report["fingerprint"]:
            raise RecoveryError("target_changed_since_preflight")
        values = (intended_after["finished_at"], intended_after["errors"], TARGET_ID,
                  row["started_at"], row["tier"], row["pax_spec"], row["params"],
                  row["errors"], row["requests_made"], row["offers_seen"], source)
        changed = conn.execute(UPDATE, values).fetchall()
        if len(changed) != 1 or dict(changed[0]) != intended_after:
            raise RecoveryError("compare_and_set_failed")
        after = conn.execute(SELECT, (TARGET_ID,)).fetchone()
        if not after or dict(after) != intended_after:
            raise RecoveryError("postcheck_failed")
    return dict(after)


def audit_write(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def public_audit(report):
    """Artifacts from a public repository must contain only this safe summary."""
    public = {key: report[key] for key in ("mode", "outcome", "checked_at", "target_id",
                                         "fingerprint", "expected", "github")}
    for name in ("before", "intended_after", "after"):
        if name not in report:
            continue
        row = report[name]
        public[name] = {key: row[key] for key in ("id", "started_at", "finished_at", "tier",
                                                  "pax_spec", "requests_made", "offers_seen")}
        public[name]["error_count"] = len(row_errors(row))
        public[name]["errors_state"] = "null" if row["errors"] is None else "json_list"
        public[name]["row_fingerprint"] = hashlib.sha256(canonical(row).encode()).hexdigest()
    return public


def save_audits(private_path, public_path, report):
    audit_write(private_path, report)
    audit_write(public_path, public_audit(report))


def database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url.startswith("postgresql+psycopg://"):
        url = "postgresql://" + url[len("postgresql+psycopg://"):]
    if not url.startswith(("postgresql://", "postgres://")):
        raise RecoveryError("explicit_postgresql_url_required")
    return url


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "apply"), default="preflight")
    parser.add_argument("--audit", type=Path, default=Path("work/legacy-recovery-1589.json"))
    parser.add_argument("--private-audit", type=Path,
                        default=Path("work/private-legacy-recovery-1589.json"),
                        help="Runner-local before/after images; never upload or publish this file")
    for key in ("fingerprint", "started-at", "source", "tier", "pax"):
        parser.add_argument("--expected-" + key, default="")
    args = parser.parse_args(argv)
    stage, report = "configuration", None
    try:
        if args.audit.resolve() == args.private_audit.resolve():
            raise RecoveryError("private_and_public_audit_paths_must_differ")
        if os.environ.get("GITHUB_REPOSITORY", REPOSITORY) != REPOSITORY:
            raise RecoveryError("wrong_repository")
        expected = {key: getattr(args, "expected_" + key)
                    for key in ("fingerprint", "started_at", "source", "tier", "pax")}
        if args.mode == "apply" and not all(expected.values()):
            raise RecoveryError("apply_requires_preflight_identity")
        github = GitHub(os.environ.get("GITHUB_TOKEN", ""))
        with psycopg.connect(database_url(), autocommit=True, row_factory=dict_row,
                             prepare_threshold=None, connect_timeout=15) as conn:
            stage = "preflight"
            report = preflight(conn, github)
            save_audits(args.private_audit, args.audit, report)
            if args.mode == "apply":
                require_expected(report, expected)
                after = {**report["before"], "finished_at": utcnow(),
                         "errors": json.dumps(row_errors(report["before"]) + [MARKER])}
                report.update(mode="apply", outcome="intent_recorded", intended_after=after)
                # If the runner stops during commit, this persisted before/after
                # intent permits an exact read-only check; never assume rollback.
                save_audits(args.private_audit, args.audit, report)
                stage = "apply"
                report["after"] = apply_recovery(conn, github, report, expected, after)
                report["outcome"] = "committed"
                stage = "audit_after_commit"
                save_audits(args.private_audit, args.audit, report)
        print(json.dumps({key: report[key] for key in
                          ("mode", "outcome", "target_id", "fingerprint", "expected")}), flush=True)
        return 0
    except Exception as exc:
        # Never log exception text, URLs, headers, environment or DB diagnostics.
        error = {"ok": False, "stage": stage, "error": type(exc).__name__,
                 "target_id": TARGET_ID}
        if isinstance(exc, RecoveryError):
            error["code"] = str(exc)
        print(json.dumps(error), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
