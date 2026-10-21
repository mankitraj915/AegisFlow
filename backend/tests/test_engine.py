import json
import threading

import pytest

from aegisflow import engine, tools
from aegisflow.db import connect
from aegisflow.models import Scenario

PIPELINE = ["fetch_order", "fetch_customer", "check_refund_policy",
            "propose_refund", "issue_refund", "notify_customer"]


def refunds(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM refunds")]


def steps(conn, run_id):
    return [dict(r) for r in conn.execute("SELECT * FROM steps WHERE run_id=? ORDER BY idx", (run_id,))]


def status(conn, run_id):
    return engine.get_run(conn, run_id).status


def crash_after_tool(point, name):
    if point == "after_tool" and name == "issue_refund":
        raise RuntimeError("simulated crash")


def test_low_value_refund_needs_no_approval(conn):
    run_id = engine.start_run(conn, "ORD-1001", Scenario())
    assert status(conn, run_id) == "succeeded"
    assert [s["name"] for s in steps(conn, run_id)] == PIPELINE
    assert all(s["status"] == "succeeded" for s in steps(conn, run_id))
    assert len(refunds(conn)) == 1 and refunds(conn)[0]["amount_paise"] == 129_900
    seqs = [e.seq for e in engine.list_events(conn, run_id)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_amount_equal_to_threshold_is_not_gated(conn):
    run_id = engine.start_run(conn, "ORD-4004", Scenario())
    assert status(conn, run_id) == "succeeded"


def test_high_value_refund_stops_at_gate(conn):
    run_id = engine.start_run(conn, "ORD-2002", Scenario())
    assert status(conn, run_id) == "awaiting_approval"
    assert refunds(conn) == []
    assert [s["name"] for s in steps(conn, run_id)] == PIPELINE[:4]
    detail = engine.get_run_detail(conn, run_id)
    assert detail.pending_approval.requested_payload["amount_paise"] == 1_200_000
    assert detail.latest_checkpoint_idx == 3


def test_threshold_is_scenario_configurable(conn):
    run_id = engine.start_run(conn, "ORD-1001", Scenario(approval_threshold_paise=100_000))
    assert status(conn, run_id) == "awaiting_approval"
    run_id = engine.start_run(conn, "ORD-2002", Scenario(approval_threshold_paise=2_000_000))
    assert status(conn, run_id) == "succeeded"


@pytest.mark.parametrize("order_id,scenario", [
    ("ORD-3003", Scenario()),                                   # order flagged
    ("ORD-1001", Scenario(require_manual_review=True)),         # scenario flagged
])
def test_manual_review_gates_low_value_refund(conn, order_id, scenario):
    run_id = engine.start_run(conn, order_id, scenario)
    assert status(conn, run_id) == "awaiting_approval"
    assert refunds(conn) == []


def test_approve_resumes_without_rerunning_earlier_steps(conn):
    run_id = engine.start_run(conn, "ORD-2002", Scenario())
    before = steps(conn, run_id)
    engine.approve(conn, run_id, "ops@example.test")
    after = steps(conn, run_id)
    assert status(conn, run_id) == "succeeded"
    assert len(refunds(conn)) == 1
    assert after[:4] == before  # untouched: same rows, same timestamps
    assert [s["name"] for s in after] == PIPELINE


def test_reject_issues_no_refund(conn):
    run_id = engine.start_run(conn, "ORD-2002", Scenario())
    engine.reject(conn, run_id, "ops@example.test", "suspicious")
    assert status(conn, run_id) == "rejected"
    assert refunds(conn) == []
    assert engine.advance(conn, run_id) == "rejected"
    assert refunds(conn) == []


def test_second_decision_conflicts(conn):
    run_id = engine.start_run(conn, "ORD-2002", Scenario())
    engine.approve(conn, run_id, "a")
    with pytest.raises(engine.Conflict):
        engine.approve(conn, run_id, "b")
    with pytest.raises(engine.Conflict):
        engine.reject(conn, run_id, "b")
    assert len(refunds(conn)) == 1


def test_concurrent_approvals_have_one_winner(db_path):
    setup = connect(db_path)
    run_id = engine.start_run(setup, "ORD-2002", Scenario())
    results = []

    def attempt(who):
        c = connect(db_path)
        try:
            engine.approve(c, run_id, who)
            results.append("ok")
        except engine.Conflict:
            results.append("conflict")
        finally:
            c.close()

    threads = [threading.Thread(target=attempt, args=(f"u{i}",)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["conflict"] * 3 + ["ok"]
    assert len(refunds(setup)) == 1
    assert len(steps(setup, run_id)) == 6


def test_crash_window_resume_creates_no_second_refund(conn):
    with pytest.raises(RuntimeError):
        engine.start_run(conn, "ORD-1001", Scenario(), hook=crash_after_tool)
    run_id = conn.execute("SELECT id FROM runs").fetchone()["id"]
    # Ledger committed, step still 'started'.
    assert len(refunds(conn)) == 1
    stuck = [s for s in steps(conn, run_id) if s["status"] == "started"]
    assert [s["name"] for s in stuck] == ["issue_refund"]
    key = stuck[0]["idempotency_key"]

    assert engine.advance(conn, run_id) == "succeeded"
    assert len(refunds(conn)) == 1
    final = steps(conn, run_id)
    assert [s["name"] for s in final] == PIPELINE  # no duplicate step row
    assert final[4]["idempotency_key"] == key and final[4]["status"] == "succeeded"


def test_crash_right_after_approval_is_recoverable(conn):
    run_id = engine.start_run(conn, "ORD-2002", Scenario())
    with pytest.raises(RuntimeError):
        engine.approve(conn, run_id, "a", hook=crash_after_tool)
    assert status(conn, run_id) == "running"
    assert engine.advance(conn, run_id) == "succeeded"
    assert len(refunds(conn)) == 1


def test_refund_ledger_is_idempotent_and_rejects_payload_mismatch(conn):
    def state(order_id, amount):
        return {"outputs": {"propose_refund": {"order_id": order_id, "amount_paise": amount}}}

    first = tools.issue_refund(conn, "k1", state("ORD-1001", 100))
    assert tools.issue_refund(conn, "k1", state("ORD-1001", 100)) == first
    assert len(refunds(conn)) == 1
    with pytest.raises(tools.ToolError):
        tools.issue_refund(conn, "k1", state("ORD-1001", 101))
    with pytest.raises(tools.ToolError):
        tools.issue_refund(conn, "k1", state("ORD-2002", 100))
    assert len(refunds(conn)) == 1 and refunds(conn)[0]["amount_paise"] == 100


def test_money_is_integer_paise_throughout(conn):
    run_id = engine.start_run(conn, "ORD-1001", Scenario())
    for s in steps(conn, run_id):
        if s["name"] in ("propose_refund", "issue_refund"):
            assert type(json.loads(s["output_json"])["amount_paise"]) is int
    assert conn.execute("SELECT typeof(amount_paise) FROM refunds").fetchone()[0] == "integer"


def test_ineligible_order_fails_run_with_history(conn):
    run_id = engine.start_run(conn, "ORD-9009", Scenario())
    run = engine.get_run(conn, run_id)
    assert run.status == "failed" and "not refundable" in run.error
    assert steps(conn, run_id)[-1]["status"] == "failed"
    assert refunds(conn) == []
    assert [e.type for e in engine.list_events(conn, run_id)][-2:] == ["step_failed", "run_failed"]


def test_unknown_order_and_run(conn):
    with pytest.raises(engine.OrderNotFound):
        engine.start_run(conn, "NOPE", Scenario())
    with pytest.raises(engine.RunNotFound):
        engine.advance(conn, "missing")


def test_persistence_across_connections(db_path):
    c1 = connect(db_path)
    run_id = engine.start_run(c1, "ORD-2002", Scenario())
    snapshot = (engine.get_run_detail(c1, run_id), engine.list_events(c1, run_id))
    c1.close()
    c2 = connect(db_path)
    assert (engine.get_run_detail(c2, run_id), engine.list_events(c2, run_id)) == snapshot
    assert c2.execute("SELECT COUNT(*) FROM checkpoints WHERE run_id=?", (run_id,)).fetchone()[0] == 4
    engine.approve(c2, run_id, "a")  # resumable from a fresh connection
    assert status(c2, run_id) == "succeeded"
    c2.close()


def test_deterministic_outputs_across_runs(conn):
    def outputs(run_id):
        # Run ids (embedded in idempotency keys / refund ids) are the only permitted difference.
        return [(s["name"], s["output_json"].replace(run_id, "<run>")) for s in steps(conn, run_id)]

    a = engine.start_run(conn, "ORD-1001", Scenario())
    b = engine.start_run(conn, "ORD-1001", Scenario())
    assert a != b and outputs(a) == outputs(b)
