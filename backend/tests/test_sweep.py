"""Abandoned runs get picked up without anyone asking."""
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from aegisflow import engine, recovery, runtime, tools
from aegisflow.api import create_app
from aegisflow.db import connect
from aegisflow.models import Scenario


def refunds(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM refunds")]


def types(conn, run_id):
    return [e.type for e in engine.list_events(conn, run_id)]


def status(conn, run_id):
    return engine.get_run(conn, run_id).status


def only_run(conn):
    return conn.execute("SELECT id FROM runs").fetchone()["id"]


def crash_at(point_, name_):
    def hook(point, name):
        if point == point_ and name == name_:
            raise RuntimeError("simulated crash")
    return hook


def later(monkeypatch, seconds):
    moved = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
    monkeypatch.setattr(engine, "now", lambda: moved)


def abandon(conn, order="ORD-1001", at=("after_tool", "issue_refund")):
    """A driver that died mid-step and never released its lease."""
    with pytest.raises(RuntimeError):
        engine.start_run(conn, order, Scenario(), hook=crash_at(*at))
    run_id = conn.execute("SELECT id FROM runs ORDER BY created_at DESC, rowid DESC").fetchone()["id"]
    expires = (datetime.now(UTC) + timedelta(seconds=engine.LEASE_S)).isoformat()
    conn.execute("UPDATE runs SET lease_token=lease_epoch, lease_expires_at=? WHERE id=?", (expires, run_id))
    return run_id


# ---- the worker ----


def test_a_sweep_finishes_a_run_whose_lease_expired(conn, monkeypatch):
    run_id = abandon(conn)
    later(monkeypatch, engine.LEASE_S + 1)

    result = recovery.sweep(conn)

    assert result.recovered == {run_id: "succeeded"}
    assert "run_recovered" in types(conn, run_id)
    assert len(refunds(conn)) == 1
    assert conn.execute("SELECT lease_token FROM runs").fetchone()[0] is None


def test_a_sweep_leaves_a_live_lease_alone(conn):
    run_id = abandon(conn)

    assert recovery.sweep(conn).recovered == {}
    assert status(conn, run_id) == "running"
    assert "run_recovered" not in types(conn, run_id)


def test_a_run_nobody_claimed_waits_out_the_grace_period_first(conn, monkeypatch):
    run_id = abandon(conn)
    conn.execute("UPDATE runs SET lease_token=NULL, lease_expires_at=NULL")  # Released, but left running.

    assert recovery.sweep(conn).recovered == {}
    later(monkeypatch, engine.LEASE_S + 1)
    assert recovery.sweep(conn).recovered == {run_id: "succeeded"}


def test_a_sweep_leaves_finished_and_waiting_runs_alone(conn, monkeypatch):
    done = engine.start_run(conn, "ORD-1001", Scenario())
    waiting = engine.start_run(conn, "ORD-2002", Scenario())
    interrupted = engine.start_run(conn, "ORD-1002", Scenario(faults={"issue_refund": ["interrupt_after_commit"]}))
    later(monkeypatch, engine.LEASE_S + 1)

    assert recovery.sweep(conn).recovered == {}
    assert (status(conn, done), status(conn, waiting)) == ("succeeded", "awaiting_approval")
    assert engine.get_run(conn, interrupted).interrupted  # A person is meant to resume this one.


def test_a_sweep_rechecks_a_held_run(conn, monkeypatch):
    def down(name, key, call):
        raise tools.RetryableToolError("status lookup unavailable")

    monkeypatch.setattr(runtime.current, "lookup", down)
    run_id = engine.start_run(conn, "ORD-1001", Scenario(faults={"issue_refund": ["retryable_after_commit"]}))
    assert recovery.sweep(conn).rechecked == {run_id: "needs_review"}  # Still can't tell.
    monkeypatch.undo()

    assert recovery.sweep(conn).rechecked == {run_id: "succeeded"}
    assert len(refunds(conn)) == 1


def test_a_replaced_driver_cannot_write_after_a_sweep_takes_over(db_path, monkeypatch):
    old = connect(db_path)
    new = connect(db_path)
    reached, go, outcome = threading.Event(), threading.Event(), []

    def stall(point, name):
        if point == "after_tool" and name == "issue_refund":
            reached.set()
            go.wait(5)

    def driver():
        try:
            outcome.append(engine.start_run(old, "ORD-1001", Scenario(), hook=stall))
        except engine.LeaseLost as exc:
            outcome.append(exc)

    thread = threading.Thread(target=driver)
    thread.start()
    assert reached.wait(5)
    moved = (datetime.now(UTC) + timedelta(seconds=engine.LEASE_S + 1)).isoformat()
    real_now = engine.now
    monkeypatch.setattr(engine, "now", lambda: moved if threading.current_thread() is not thread else real_now())

    run_id = only_run(new)
    assert recovery.sweep(new).recovered == {run_id: "succeeded"}
    history = types(new, run_id)
    go.set()
    thread.join(5)

    assert isinstance(outcome[0], engine.LeaseLost)
    assert types(new, run_id) == history  # Not one event from the driver it replaced.
    assert len(refunds(new)) == 1
    old.close()
    new.close()


def test_one_bad_run_does_not_stop_a_sweep(conn, monkeypatch):
    bad = abandon(conn, "ORD-1001")
    good = abandon(conn, "ORD-1002")
    later(monkeypatch, engine.LEASE_S + 1)
    real = engine.recover

    def recover(conn, run_id, hook=None):
        if run_id == bad:
            raise ValueError("a bug in this run's step")
        return real(conn, run_id, hook)

    monkeypatch.setattr(engine, "recover", recover)
    result = recovery.sweep(conn)

    assert result.recovered == {good: "succeeded"}
    assert [run_id for run_id, _ in result.errors] == [bad]


# ---- real processes ----

CHILD = """
import sys, time
from aegisflow import engine
from aegisflow.db import connect
from aegisflow.models import Scenario

conn = connect(sys.argv[1])

def park(point, name):
    if point == "after_tool" and name == "issue_refund":
        run_id = conn.execute("SELECT id FROM runs").fetchone()[0]
        print(run_id, flush=True)
        time.sleep(60)

engine.start_run(conn, "ORD-1001", Scenario(), hook=park)
"""


def test_a_sweep_recovers_after_real_process_death(db_path, monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", CHILD, db_path], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        run_id = child.stdout.readline().strip()
        assert run_id, child.stderr.read()
    finally:
        child.kill()
        child.wait(10)

    conn = connect(db_path)
    assert recovery.sweep(conn).recovered == {}  # Its lease hasn't run out yet.
    later(monkeypatch, engine.LEASE_S + 1)
    assert recovery.sweep(conn).recovered == {run_id: "succeeded"}
    assert len(refunds(conn)) == 1
    conn.close()


def test_the_server_sweeps_in_the_background_while_it_is_up(db_path, monkeypatch):
    conn = connect(db_path)
    run_id = abandon(conn)
    conn.execute("UPDATE runs SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    monkeypatch.setenv("AEGISFLOW_RECOVERY_INTERVAL_S", "0.01")

    with TestClient(create_app(db_path)):
        deadline = time.monotonic() + 10
        while status(conn, run_id) != "succeeded" and time.monotonic() < deadline:
            time.sleep(0.01)

    assert status(conn, run_id) == "succeeded"
    conn.close()


def test_the_background_sweep_can_be_turned_off(db_path, monkeypatch):
    conn = connect(db_path)
    run_id = abandon(conn)
    conn.execute("UPDATE runs SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    monkeypatch.setenv("AEGISFLOW_RECOVERY_INTERVAL_S", "0")

    with TestClient(create_app(db_path)):
        assert threading.active_count() >= 1
    assert status(conn, run_id) == "running"
    conn.close()
