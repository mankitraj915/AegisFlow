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


def test_backoff_survives_a_crash(conn, sleeps, monkeypatch):
    # The clock stands still, so the whole delay is still owed when the run resumes.
    monkeypatch.setattr(engine, "now", lambda: "2026-01-01T00:00:00+00:00")
    with pytest.raises(RuntimeError):
        engine.start_run(conn, "ORD-1001", faults(fetch_customer=["retryable"]),
                         hook=crash_at("retry_scheduled", "fetch_customer"))
    run_id = only_run(conn)
    assert sleeps == []  # The crash came before the wait.
    assert engine.advance(conn, run_id) == "succeeded"
    assert sleeps == [pytest.approx(0.1)]


def test_backoff_already_over_is_not_waited_for_again(conn, sleeps, monkeypatch):
    with pytest.raises(RuntimeError):
        engine.start_run(conn, "ORD-1001", faults(fetch_customer=["retryable"]),
                         hook=crash_at("retry_scheduled", "fetch_customer"))
    run_id = only_run(conn)
    next_at = conn.execute("SELECT next_attempt_at FROM steps WHERE run_id=? AND idx=1", (run_id,)).fetchone()[0]
    monkeypatch.setattr(engine, "now", lambda: next_at)
    assert engine.advance(conn, run_id) == "succeeded"
    assert sleeps == []


def test_backoff_wait_is_capped_if_the_clock_jumped_back(conn, sleeps, monkeypatch):
    with pytest.raises(RuntimeError):
        engine.start_run(conn, "ORD-1001", faults(fetch_customer=["retryable"]),
                         hook=crash_at("retry_scheduled", "fetch_customer"))
    run_id = only_run(conn)
    monkeypatch.setattr(engine, "now", lambda: "2000-01-01T00:00:00+00:00")
    conn.execute("UPDATE runs SET lease_token=NULL")
    assert engine.advance(conn, run_id) == "succeeded"
    assert sleeps == [engine.RETRY_POLICY.max_delay_s]


def test_exhaustion_with_nothing_found_holds_the_run_instead_of_failing_it(conn):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"] * 3))
    assert status(conn, run_id) == "needs_review"
    key = step(conn, run_id, "issue_refund")["idempotency_key"]
    payload = events(conn, run_id, "run_needs_review")[0].payload
    assert payload["outcome_unknown"] is True and payload["idempotency_key"] == key
    assert not events(conn, run_id, "run_failed") and not refunds(conn)
    assert [x["name"] for x in steps(conn, run_id)] == PIPELINE[:5]  # never notifies
    assert engine.advance(conn, run_id) == "needs_review"
    assert not refunds(conn) and len(steps(conn, run_id)) == 5


def test_a_refund_committed_before_a_timeout_is_recorded_once_whatever_was_planned_next(conn):
    plans = ((["retryable_after_commit"] * 3, "ORD-1001"), (["retryable_after_commit", "fatal"], "ORD-1002"))
    for plan, order in plans:
        run_id = engine.start_run(conn, order, faults(issue_refund=plan))
        assert status(conn, run_id) == "succeeded"  # The lookup settled it; attempt 2 never ran.
        assert step(conn, run_id, "issue_refund")["attempt"] == 2
    assert len(refunds(conn)) == 2


def test_resuming_after_a_crash_finds_the_refund_before_any_planned_fault_can_fire(conn):
    def crash(point, name):
        if point == "after_tool" and name == "issue_refund":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        engine.start_run(conn, "ORD-1001", Scenario(), hook=crash)
    run_id = only_run(conn)
    conn.execute("UPDATE runs SET scenario_json=? WHERE id=?",
                 (faults(issue_refund=["fatal"]).model_dump_json(), run_id))
    assert engine.advance(conn, run_id) == "succeeded"
    assert len(refunds(conn)) == 1


def test_failure_after_the_refund_names_the_committed_refund(conn):
    run_id = engine.start_run(conn, "ORD-1001", faults(notify_customer=["fatal"]))
    assert status(conn, run_id) == "failed"
    key = step(conn, run_id, "issue_refund")["idempotency_key"]
    assert events(conn, run_id, "run_failed")[0].payload["effects_committed"] == [key]
    assert [r["idempotency_key"] for r in refunds(conn)] == [key]


def test_failure_before_the_refund_names_nothing(conn):
    run_id = engine.start_run(conn, "ORD-1001", faults(fetch_customer=["fatal"]))
    assert "effects_committed" not in events(conn, run_id, "run_failed")[0].payload


def test_refund_retry_after_approval_does_not_regate(conn):
    run_id = engine.start_run(conn, "ORD-2002", faults(issue_refund=["retryable", "retryable_after_commit"]))
    assert status(conn, run_id) == "awaiting_approval"
    assert engine.approve(conn, run_id, "ops") == "succeeded"
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    assert len(refunds(conn)) == 1
    assert step(conn, run_id, "issue_refund")["attempt"] == 3


def test_retries_are_deterministic_across_runs(conn):
    scenario = faults(fetch_order=["retryable"], issue_refund=["retryable_after_commit"])

    def history(run_id, order_id):
        return [(e.type, json.dumps(e.payload, sort_keys=True).replace(run_id, "<run>").replace(order_id, "<order>"))
                for e in events(conn, run_id)]

    a = engine.start_run(conn, "ORD-1001", scenario)
    b = engine.start_run(conn, "ORD-1002", scenario)
    assert history(a, "ORD-1001") == history(b, "ORD-1002")


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
