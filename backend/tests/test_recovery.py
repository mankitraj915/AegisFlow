import threading

import pytest

from aegisflow import engine
from aegisflow.db import connect
from aegisflow.models import Scenario

PIPELINE = ["fetch_order", "fetch_customer", "check_refund_policy",
            "propose_refund", "issue_refund", "notify_customer"]

INTERRUPT_REFUND = Scenario(faults={"issue_refund": ["interrupt_after_commit"]})


def refunds(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM refunds")]


def steps(conn, run_id):
    return [dict(r) for r in conn.execute("SELECT * FROM steps WHERE run_id=? ORDER BY idx", (run_id,))]


def events(conn, run_id):
    return engine.list_events(conn, run_id)


def lease(conn, run_id):
    return conn.execute("SELECT lease_token FROM runs WHERE id=?", (run_id,)).fetchone()[0]


def test_interruption_after_refund_commit_leaves_run_resumable(conn):
    run_id = engine.start_run(conn, "ORD-1001", INTERRUPT_REFUND)
    assert engine.get_run(conn, run_id).status == "running"
    assert lease(conn, run_id) is None
    # Ledger committed; the step is not recorded as succeeded.
    [refund] = refunds(conn)
    stuck = steps(conn, run_id)[4]
    assert (stuck["name"], stuck["status"], stuck["attempt"]) == ("issue_refund", "started", 1)
    key = stuck["idempotency_key"]
    assert refund["idempotency_key"] == key
    last = events(conn, run_id)[-1]
    assert last.type == "run_interrupted"
    assert last.payload == {"idx": 4, "name": "issue_refund", "attempt": 1, "idempotency_key": key, "simulated": True}

    assert engine.resume(conn, run_id) == "succeeded"
    assert refunds(conn) == [refund]
    final = steps(conn, run_id)
    assert [s["name"] for s in final] == PIPELINE
    assert all(s["status"] == "succeeded" for s in final)
    assert (final[4]["idempotency_key"], final[4]["attempt"]) == (key, 1)
    assert final[4]["output_json"] is not None and key in final[4]["output_json"]

    history = events(conn, run_id)
    types = [e.type for e in history]
    refund_events = [(e.type, e.payload.get("idempotency_key")) for e in history if e.payload.get("idx") == 4]
    assert refund_events == [
        ("step_started", None), ("run_interrupted", key), ("run_resumed", key), ("step_succeeded", None),
    ]
    assert types.count("run_interrupted") == 1 and types[-1] == "run_succeeded"
    assert [e.seq for e in history] == list(range(1, len(history) + 1))
    assert lease(conn, run_id) is None


def test_resume_only_applies_to_running_runs(conn):
    done = engine.start_run(conn, "ORD-1001", Scenario())
    gated = engine.start_run(conn, "ORD-2002", Scenario())
    for run_id in (done, gated):
        with pytest.raises(engine.Conflict, match="only a running run can be resumed"):
            engine.resume(conn, run_id)
    with pytest.raises(engine.RunNotFound):
        engine.resume(conn, "nope")
    assert len(refunds(conn)) == 1


def test_interruption_after_approval_is_resumable(conn):
    run_id = engine.start_run(conn, "ORD-2002", INTERRUPT_REFUND)
    assert engine.approve(conn, run_id, "ops") == "running"
    assert engine.resume(conn, run_id) == "succeeded"
    assert len(refunds(conn)) == 1
    assert [s["name"] for s in steps(conn, run_id)] == PIPELINE


def test_concurrent_advance_is_refused_while_run_is_held(db_path):
    setup = connect(db_path)
    run_id = engine.start_run(setup, "ORD-1001", INTERRUPT_REFUND)
    inside, release, results = threading.Event(), threading.Event(), []

    def hold(point, name):
        if point == "after_tool" and name == "issue_refund":
            inside.set()
            release.wait(5)

    def first():
        c = connect(db_path)
        try:
            results.append(engine.resume(c, run_id, hook=hold))
        finally:
            c.close()

    t = threading.Thread(target=first)
    t.start()
    assert inside.wait(5)
    other = connect(db_path)
    for call in (engine.resume, engine.advance):
        with pytest.raises(engine.Conflict, match="already being advanced"):
            call(other, run_id)
    release.set()
    t.join()
    other.close()

    assert results == ["succeeded"]
    assert len(refunds(setup)) == 1
    assert [s["name"] for s in steps(setup, run_id)] == PIPELINE
    assert [e.type for e in events(setup, run_id)].count("run_resumed") == 1
    setup.close()


def test_lease_is_released_on_crash_and_expires_after_a_kill(conn):
    def crash(point, name):
        if point == "after_tool" and name == "issue_refund":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        engine.start_run(conn, "ORD-1001", Scenario(), hook=crash)
    run_id = conn.execute("SELECT id FROM runs").fetchone()["id"]
    assert lease(conn, run_id) is None

    # A process killed mid-advance leaves its lease; it blocks until it expires.
    conn.execute("UPDATE runs SET lease_token='dead', lease_expires_at='9999-01-01T00:00:00+00:00' WHERE id=?",
                 (run_id,))
    with pytest.raises(engine.Conflict):
        engine.resume(conn, run_id)
    conn.execute("UPDATE runs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (run_id,))
    assert engine.resume(conn, run_id) == "succeeded"
    assert len(refunds(conn)) == 1 and lease(conn, run_id) is None


def test_interrupted_flag_follows_persisted_events(conn):
    def crash(point, name):
        if point == "after_tool" and name == "issue_refund":
            raise RuntimeError("simulated crash")

    plain = engine.start_run(conn, "ORD-1001", Scenario())
    run_id = engine.start_run(conn, "ORD-1001", INTERRUPT_REFUND)
    flags = lambda: {r.id: (r.status, r.interrupted) for r in engine.list_runs(conn)}
    assert flags() == {plain: ("succeeded", False), run_id: ("running", True)}
    assert engine.get_run(conn, run_id).interrupted and engine.get_run_detail(conn, run_id).interrupted

    # Once resumed, the run shows its actual state, even if that resume itself crashes.
    with pytest.raises(RuntimeError):
        engine.resume(conn, run_id, hook=crash)
    assert flags()[run_id] == ("running", False)
    assert engine.resume(conn, run_id) == "succeeded"
    assert flags()[run_id] == ("succeeded", False)
