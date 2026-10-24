import json
import sqlite3

import pytest

from aegisflow import db, engine
from aegisflow.db import connect
from aegisflow.models import Scenario

PIPELINE = ["fetch_order", "fetch_customer", "check_refund_policy",
            "propose_refund", "issue_refund", "notify_customer"]


def refunds(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM refunds")]


def steps(conn, run_id):
    return [dict(r) for r in conn.execute("SELECT * FROM steps WHERE run_id=? ORDER BY idx", (run_id,))]


def step(conn, run_id, name):
    return next(s for s in steps(conn, run_id) if s["name"] == name)


def events(conn, run_id, type_=None):
    return [e for e in engine.list_events(conn, run_id) if type_ is None or e.type == type_]


def status(conn, run_id):
    return engine.get_run(conn, run_id).status


def faults(**plan):
    return Scenario(faults=plan)


def crash_at(point_, name_):
    def hook(point, name):
        if point == point_ and name == name_:
            raise RuntimeError("simulated crash")
    return hook


def only_run(conn):
    return conn.execute("SELECT id FROM runs").fetchone()["id"]


def test_transient_failures_are_retried_to_success(conn, sleeps):
    run_id = engine.start_run(conn, "ORD-1001", faults(fetch_customer=["retryable", "retryable"]))
    assert status(conn, run_id) == "succeeded"
    s = step(conn, run_id, "fetch_customer")
    assert s["status"] == "succeeded" and s["attempt"] == 3
    assert [x["name"] for x in steps(conn, run_id)] == PIPELINE
    assert sleeps == [0.1, 0.2]
    failed = [e.payload for e in events(conn, run_id, "step_attempt_failed")]
    assert [p["attempt"] for p in failed] == [1, 2]
    assert [p["next_delay_s"] for p in failed] == sleeps
    assert all(p["retryable"] and "injected transient fault" in p["error"] for p in failed)
    assert len(refunds(conn)) == 1


def test_retry_exhaustion_fails_run_and_stops(conn, sleeps):
    run_id = engine.start_run(conn, "ORD-1001", faults(fetch_customer=["retryable"] * 3))
    run = engine.get_run(conn, run_id)
    assert run.status == "failed" and "retries exhausted after 3 attempts" in run.error
    assert [x["name"] for x in steps(conn, run_id)] == PIPELINE[:2]
    s = step(conn, run_id, "fetch_customer")
    assert s["status"] == "failed" and s["attempt"] == 3
    types = [e.type for e in events(conn, run_id)]
    assert types[-4:] == ["step_attempt_failed", "step_attempt_failed", "step_failed", "run_failed"]
    last = events(conn, run_id, "step_failed")[0].payload
    assert last["exhausted"] and last["attempt"] == 3 and "outcome_unknown" not in last
    assert sleeps == [0.1, 0.2]
    assert refunds(conn) == []
    assert engine.advance(conn, run_id) == "failed"


@pytest.mark.parametrize("name", ["fetch_order", "issue_refund"])
def test_fatal_error_is_not_retried(conn, sleeps, name):
    run_id = engine.start_run(conn, "ORD-1001", faults(**{name: ["fatal"]}))
    run = engine.get_run(conn, run_id)
    assert run.status == "failed" and "injected fatal fault" in run.error
    s = step(conn, run_id, name)
    assert s["status"] == "failed" and s["attempt"] == 1
    payload = events(conn, run_id, "step_failed")[0].payload
    assert payload["retryable"] is False and "outcome_unknown" not in payload
    assert sleeps == [] and refunds(conn) == []
    assert events(conn, run_id, "step_attempt_failed") == []


def test_backoff_is_exponential_capped_and_deterministic():
    assert [engine.RETRY_POLICY.delay(a) for a in (1, 2, 3)] == [0.1, 0.2, 0.4]
    policy = engine.RetryPolicy(max_attempts=5, base_delay_s=1, max_delay_s=3)
    assert [policy.delay(a) for a in (1, 2, 3, 4)] == [1, 2, 3, 3]


def test_retry_policy_bounds_attempts(conn, sleeps, monkeypatch):
    monkeypatch.setattr(engine, "RETRY_POLICY", engine.RetryPolicy(max_attempts=1))
    run_id = engine.start_run(conn, "ORD-1001", faults(fetch_order=["retryable"]))
    assert status(conn, run_id) == "failed"
    assert step(conn, run_id, "fetch_order")["attempt"] == 1 and sleeps == []

    monkeypatch.setattr(engine, "RETRY_POLICY", engine.RetryPolicy(max_attempts=4, base_delay_s=0.5))
    run_id = engine.start_run(conn, "ORD-1001", faults(fetch_order=["retryable"] * 3))
    assert status(conn, run_id) == "succeeded"
    assert sleeps == [0.5, 1.0, 2.0]


def test_failure_after_refund_commit_retries_with_same_key(conn):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable_after_commit"]))
    assert status(conn, run_id) == "succeeded"
    s = step(conn, run_id, "issue_refund")
    assert s["status"] == "succeeded" and s["attempt"] == 2
    assert s["idempotency_key"] == f"{run_id}:4"
    ledger = refunds(conn)
    assert len(ledger) == 1 and ledger[0]["idempotency_key"] == s["idempotency_key"]
    assert json.loads(s["output_json"])["refund_id"] == s["idempotency_key"]


def test_crash_after_refund_commit_on_retry_attempt(conn):
    with pytest.raises(RuntimeError):
        engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"]),
                         hook=crash_at("after_tool", "issue_refund"))
    run_id = only_run(conn)
    s = step(conn, run_id, "issue_refund")
    assert s["status"] == "started" and s["attempt"] == 2
    assert len(refunds(conn)) == 1

    assert engine.advance(conn, run_id) == "succeeded"
    assert len(refunds(conn)) == 1
    final = steps(conn, run_id)
    assert [x["name"] for x in final] == PIPELINE
    assert final[4]["idempotency_key"] == s["idempotency_key"] and final[4]["attempt"] == 2


def test_crash_during_backoff_keeps_retry_budget(conn, sleeps):
    with pytest.raises(RuntimeError):
        engine.start_run(conn, "ORD-1001", faults(fetch_customer=["retryable", "retryable", "retryable"]),
                         hook=crash_at("retry_scheduled", "fetch_customer"))
    run_id = only_run(conn)
    assert step(conn, run_id, "fetch_customer")["attempt"] == 2 and sleeps == []
    # Resumes at attempt 2, not 1: the run exhausts at 3 instead of getting a fresh budget.
    assert engine.advance(conn, run_id) == "failed"
    assert step(conn, run_id, "fetch_customer")["attempt"] == 3


def test_exhaustion_after_refund_commit_marks_outcome_unknown(conn):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable_after_commit"] * 3))
    assert status(conn, run_id) == "failed"
    key = step(conn, run_id, "issue_refund")["idempotency_key"]
    for type_ in ("step_failed", "run_failed"):
        payload = events(conn, run_id, type_)[0].payload
        assert payload["outcome_unknown"] is True and payload["idempotency_key"] == key
    ledger = refunds(conn)
    assert len(ledger) == 1 and ledger[0]["idempotency_key"] == key
    assert [x["name"] for x in steps(conn, run_id)] == PIPELINE[:5]  # never notifies
    assert engine.advance(conn, run_id) == "failed"
    assert len(refunds(conn)) == 1 and len(steps(conn, run_id)) == 5


def test_refund_retry_after_approval_does_not_regate(conn):
    run_id = engine.start_run(conn, "ORD-2002", faults(issue_refund=["retryable", "retryable_after_commit"]))
    assert status(conn, run_id) == "awaiting_approval"
    assert engine.approve(conn, run_id, "ops") == "succeeded"
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    assert len(refunds(conn)) == 1
    assert step(conn, run_id, "issue_refund")["attempt"] == 3


def test_retries_are_deterministic_across_runs(conn):
    scenario = faults(fetch_order=["retryable"], issue_refund=["retryable_after_commit"])

    def history(run_id):
        return [(e.type, json.dumps(e.payload, sort_keys=True).replace(run_id, "<run>"))
                for e in events(conn, run_id)]

    a = engine.start_run(conn, "ORD-1001", scenario)
    b = engine.start_run(conn, "ORD-1001", scenario)
    assert history(a) == history(b)


def test_unknown_fault_step_is_rejected():
    with pytest.raises(ValueError):
        Scenario(faults={"nope": ["retryable"]})
    with pytest.raises(ValueError):
        Scenario(faults={"fetch_order": ["flaky"]})


def test_milestone1_database_is_migrated_and_resumable(db_path):
    old_schema = db.SCHEMA.replace("    attempt         INTEGER NOT NULL DEFAULT 1,\n", "").replace(
        "    updated_at    TEXT NOT NULL,\n    -- Held while one advance() drives the run; see engine._claim.\n"
        "    lease_token      TEXT,\n    lease_expires_at TEXT\n", "    updated_at    TEXT NOT NULL\n")
    assert "attempt" not in old_schema and "lease" not in old_schema
    raw = sqlite3.connect(db_path)
    raw.executescript(old_schema)
    run_id = "legacy"
    scenario = {"approval_threshold_paise": 500_000, "require_manual_review": False}  # no 'faults'
    raw.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                (run_id, "ORD-1001", json.dumps(scenario), "running", None, "t0", "t0"))
    state = {"order_id": "ORD-1001", "scenario": scenario, "outputs": {}}
    raw.execute("INSERT INTO steps(run_id, idx, name, status, input_json, idempotency_key, started_at) "
                "VALUES (?,?,?,?,?,?,?)", (run_id, 0, "fetch_order", "started", json.dumps(state), f"{run_id}:0", "t0"))
    raw.commit()
    raw.close()

    conn = connect(db_path)
    connect(db_path).close()  # migration is idempotent
    assert step(conn, run_id, "fetch_order")["attempt"] == 1
    assert engine.advance(conn, run_id) == "succeeded"
    assert [x["name"] for x in steps(conn, run_id)] == PIPELINE
    assert engine.get_run_detail(conn, run_id).steps[0].attempt == 1
    conn.close()
