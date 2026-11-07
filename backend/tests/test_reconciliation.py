"""An error from a side-effecting call proves nothing, so the engine asks the provider.

These tests check what ends up true at the provider and in the run's history
when the provider's replies can't be believed.
"""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from aegisflow import engine, runtime, tools
from aegisflow.api import create_app
from aegisflow.models import Scenario


def refunds(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM refunds")]


def messages(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM notifications")]


def types(conn, run_id):
    return [e.type for e in engine.list_events(conn, run_id)]


def event(conn, run_id, type_):
    return next(e.payload for e in engine.list_events(conn, run_id) if e.type == type_)


def step(conn, run_id, name):
    return conn.execute("SELECT * FROM steps WHERE run_id=? AND name=?", (run_id, name)).fetchone()


def only_run(conn):
    return conn.execute("SELECT id FROM runs").fetchone()["id"]


def faults(**plan):
    return Scenario(faults=plan)


@pytest.fixture
def tool_calls(monkeypatch):
    """Every call that reaches a tool, in order."""
    calls = []
    real = runtime.current.invoke

    def counting(name, key, call):
        calls.append(name)
        return real(name, key, call)

    monkeypatch.setattr(runtime.current, "invoke", counting)
    return calls


def provider_errors_after_doing_it(monkeypatch, exc, step_name="issue_refund"):
    """The provider executes the request, then reports an error for it."""
    real = runtime.current.invoke

    def lying(name, key, call):
        output = real(name, key, call)
        if name == step_name:
            raise exc
        return output

    monkeypatch.setattr(runtime.current, "invoke", lying)


def status_lookup_fails(monkeypatch):
    def down(name, key, call):
        raise tools.RetryableToolError("status lookup unavailable")

    monkeypatch.setattr(runtime.current, "lookup", down)


def later(monkeypatch, seconds):
    moved = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
    monkeypatch.setattr(engine, "now", lambda: moved)


# ---- the provider says what happened ----


def test_a_refund_whose_response_was_lost_is_found_not_repeated(conn, tool_calls):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable_after_commit"]))

    assert engine.get_run(conn, run_id).status == "succeeded"
    assert tool_calls.count("issue_refund") == 1  # The retry never reached the provider.
    assert len(refunds(conn)) == 1
    history = types(conn, run_id)
    assert history.index("step_attempt_failed") < history.index("step_reconciled")
    assert step(conn, run_id, "issue_refund")["attempt"] == 2


def test_a_refund_the_provider_reported_as_failed_but_did_is_recorded_as_done(conn, monkeypatch, tool_calls):
    provider_errors_after_doing_it(monkeypatch, tools.ToolError("provider returned 500"))
    run_id = engine.start_run(conn, "ORD-1001", Scenario())

    assert engine.get_run(conn, run_id).status == "succeeded"
    assert event(conn, run_id, "step_reconciled")["reported_error"] == "provider returned 500"
    assert len(refunds(conn)) == 1


def test_an_exhausted_refund_that_happened_is_recorded_as_done(conn, monkeypatch):
    provider_errors_after_doing_it(monkeypatch, tools.RetryableToolError("timed out"))
    run_id = engine.start_run(conn, "ORD-1001", Scenario())

    assert engine.get_run(conn, run_id).status == "succeeded"
    assert len(refunds(conn)) == 1


def test_a_crash_after_the_refund_is_settled_by_asking_not_by_calling_again(conn, tool_calls):
    def crash(point, name):
        if point == "after_tool" and name == "issue_refund":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        engine.start_run(conn, "ORD-1001", Scenario(), hook=crash)
    run_id = only_run(conn)
    conn.execute("UPDATE runs SET lease_token=NULL")

    assert engine.resume(conn, run_id) == "succeeded"
    assert tool_calls.count("issue_refund") == 1
    assert len(refunds(conn)) == 1


def test_a_notification_is_also_found_rather_than_sent_again(conn, tool_calls):
    run_id = engine.start_run(conn, "ORD-1001", faults(notify_customer=["retryable_after_commit"]))

    assert engine.get_run(conn, run_id).status == "succeeded"
    assert tool_calls.count("notify_customer") == 1
    assert len(messages(conn)) == 1
    assert "step_reconciled" in types(conn, run_id)


def test_no_record_on_a_young_key_retries_under_the_same_key(conn, tool_calls):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"]))

    assert engine.get_run(conn, run_id).status == "succeeded"
    assert "step_reconciled" not in types(conn, run_id)
    assert [r["idempotency_key"] for r in refunds(conn)] == [f"{run_id}:4"]


def test_a_refusal_on_the_first_execution_with_nothing_at_the_provider_is_a_known_failure(conn):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["fatal"]))

    assert engine.get_run(conn, run_id).status == "failed"
    assert "run_needs_review" not in types(conn, run_id)
    assert refunds(conn) == []


# ---- when even the provider can't say ----


def assert_held(conn, run_id, reason):
    run = engine.get_run(conn, run_id)
    assert run.status == "needs_review"
    assert reason in run.error
    held = event(conn, run_id, "run_needs_review")
    assert held["outcome_unknown"] is True
    assert held["idempotency_key"] == f"{run_id}:4"
    assert step(conn, run_id, "issue_refund")["status"] == "started"
    assert step(conn, run_id, "notify_customer") is None  # Nothing later runs.
    assert "run_failed" not in types(conn, run_id)


def test_running_out_of_retries_with_no_record_is_not_reported_as_failure(conn):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"] * 3))
    # The last request may still be in flight, so "no record" proves nothing.
    assert_held(conn, run_id, "an earlier request may still arrive")


def test_a_refusal_on_a_retry_with_no_record_is_not_reported_as_failure(conn):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable", "fatal"]))
    assert_held(conn, run_id, "an earlier request may still arrive")


def test_a_status_lookup_that_fails_holds_the_run_and_makes_no_further_call(conn, monkeypatch, tool_calls):
    status_lookup_fails(monkeypatch)
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable_after_commit"]))

    assert_held(conn, run_id, "status lookup failed")
    assert tool_calls.count("issue_refund") == 1
    assert len(refunds(conn)) == 1  # Held, while the money did move.


def test_the_scenario_can_take_the_status_lookup_down(conn):
    scenario = Scenario(faults={"issue_refund": ["retryable_after_commit"]}, provider_status_down=True)
    run_id = engine.start_run(conn, "ORD-1001", scenario)
    assert_held(conn, run_id, "status lookup failed")


def test_no_record_of_an_old_key_is_not_believed(conn, monkeypatch, tool_calls):
    def crash(point, name):
        if point == "retry_scheduled" and name == "issue_refund":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"]), hook=crash)
    run_id = only_run(conn)
    later(monkeypatch, engine.KEY_TRUST_S + 1)

    assert engine.resume(conn, run_id) == "needs_review"
    assert_held(conn, run_id, "may be forgotten")
    assert tool_calls.count("issue_refund") == 0  # Repeating it could pay twice.


def test_a_provider_record_that_disagrees_is_not_taken_as_this_steps_result(conn, monkeypatch):
    def wrong(self, key):
        return {"order_id": "ORD-1001", "amount_paise": 1}

    monkeypatch.setattr(tools.SqlLedger, "refund_status", wrong)
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"]))
    assert_held(conn, run_id, "provider holds ORD-1001/1")


def test_nothing_but_settling_moves_a_held_run(conn):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"] * 3))

    assert engine.advance(conn, run_id) == "needs_review"
    with pytest.raises(engine.Conflict):
        engine.resume(conn, run_id)
    with pytest.raises(engine.Conflict):
        engine.approve(conn, run_id, "someone")
    assert engine.get_run(conn, run_id).status == "needs_review"
    assert refunds(conn) == []


# ---- settling a held run ----


def test_recheck_finds_the_effect_and_the_run_carries_on(conn, monkeypatch):
    status_lookup_fails(monkeypatch)
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable_after_commit"]))
    assert engine.get_run(conn, run_id).status == "needs_review"
    monkeypatch.undo()

    assert engine.reconcile(conn, run_id) == "succeeded"
    assert len(refunds(conn)) == 1 and len(messages(conn)) == 1
    assert event(conn, run_id, "run_review_cleared")["by"] == "provider"


@pytest.mark.parametrize("still", ["no record", "lookup down"])
def test_recheck_without_the_effect_keeps_the_run_held(conn, monkeypatch, still):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"] * 3))
    if still == "lookup down":
        status_lookup_fails(monkeypatch)

    assert engine.reconcile(conn, run_id) == "needs_review"
    assert engine.get_run(conn, run_id).status == "needs_review"
    assert refunds(conn) == []
    assert conn.execute("SELECT lease_token FROM runs").fetchone()[0] is None


def test_a_person_can_confirm_the_effect_did_not_happen(conn):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"] * 3))

    assert engine.resolve(conn, run_id, "asha", "not_applied") == "failed"
    assert event(conn, run_id, "run_failed")["resolved_by"] == "asha"
    assert step(conn, run_id, "issue_refund")["status"] == "failed"
    assert refunds(conn) == [] and messages(conn) == []


def test_a_person_can_confirm_an_effect_the_provider_has_forgotten(conn, monkeypatch):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"] * 3))

    assert engine.resolve(conn, run_id, "asha", "applied") == "succeeded"
    output = engine.get_run_detail(conn, run_id).steps[4].output
    assert output["refund_id"] == f"{run_id}:4" and output["amount_paise"] > 0
    assert event(conn, run_id, "run_review_cleared")["by"] == "asha"
    assert len(messages(conn)) == 1  # Later steps carry on.


def test_a_person_cannot_deny_an_effect_the_provider_shows(conn, monkeypatch):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"] * 3))
    tools.SqlLedger(conn).issue(f"{run_id}:4", "ORD-1001", step_amount(conn, run_id))  # It arrived after all.

    with pytest.raises(engine.Conflict, match="provider shows"):
        engine.resolve(conn, run_id, "asha", "not_applied")
    assert engine.get_run(conn, run_id).status == "needs_review"
    assert conn.execute("SELECT lease_token FROM runs").fetchone()[0] is None


def step_amount(conn, run_id):
    return engine.get_run_detail(conn, run_id).steps[3].output["amount_paise"]


def test_only_a_held_run_can_be_settled(conn):
    run_id = engine.start_run(conn, "ORD-1001", Scenario())
    with pytest.raises(engine.Conflict):
        engine.reconcile(conn, run_id)
    with pytest.raises(engine.Conflict):
        engine.resolve(conn, run_id, "asha", "applied")
    assert engine.get_run(conn, run_id).status == "succeeded"


def test_settling_needs_the_lease_like_everything_else(conn):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"] * 3))
    conn.execute("UPDATE runs SET lease_token=99, lease_expires_at='2999-01-01T00:00:00+00:00'")

    with pytest.raises(engine.Conflict, match="already being advanced"):
        engine.resolve(conn, run_id, "asha", "not_applied")
    with pytest.raises(engine.Conflict, match="already being advanced"):
        engine.reconcile(conn, run_id)
    assert engine.get_run(conn, run_id).status == "needs_review"


def test_settling_in_the_lookup_window_loses_to_a_newer_driver(conn, monkeypatch):
    run_id = engine.start_run(conn, "ORD-1001", faults(issue_refund=["retryable"] * 3))
    real = runtime.current.lookup

    def taken_over(name, key, call):
        conn.execute("UPDATE runs SET lease_epoch=lease_epoch+1")  # A newer claim, while this one was waiting.
        return real(name, key, call)

    monkeypatch.setattr(runtime.current, "lookup", taken_over)
    with pytest.raises(engine.LeaseLost):
        engine.resolve(conn, run_id, "asha", "not_applied")
    assert engine.get_run(conn, run_id).status == "needs_review"


# ---- over HTTP ----


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path))


def held_run(client):
    response = client.post("/runs", json={
        "order_id": "ORD-1001", "scenario": {"faults": {"issue_refund": ["retryable", "retryable", "retryable"]}},
    })
    assert response.status_code == 201 and response.json()["status"] == "needs_review"
    return response.json()["id"]


def test_recheck_and_resolve_over_http(client):
    run_id = held_run(client)
    assert client.post(f"/runs/{run_id}/reconcile").json()["status"] == "needs_review"
    response = client.post(f"/runs/{run_id}/resolve", json={"decided_by": "asha", "outcome": "not_applied"})
    assert response.status_code == 200 and response.json()["status"] == "failed"
    assert client.post(f"/runs/{run_id}/reconcile").status_code == 409


def test_resolve_rejects_an_unknown_outcome(client):
    run_id = held_run(client)
    response = client.post(f"/runs/{run_id}/resolve", json={"decided_by": "asha", "outcome": "maybe"})
    assert response.status_code == 422
    assert client.get(f"/runs/{run_id}").json()["status"] == "needs_review"


def test_settling_a_run_that_does_not_exist_is_404(client):
    assert client.post("/runs/nope/reconcile").status_code == 404
    assert client.post("/runs/nope/resolve", json={"decided_by": "a", "outcome": "applied"}).status_code == 404
