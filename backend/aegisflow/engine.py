"""Run engine. The only module that writes runs/steps/events/checkpoints/approvals.

Recovery model: advance() is re-entrant. State is rebuilt from succeeded steps;
a step left in 'started' (crash between tool call and completion) is resumed
with the same idempotency key rather than re-inserted.
"""
import json
import sqlite3
import uuid
from collections.abc import Callable

from . import planner, tools
from .db import now, tx
from .models import (
    ApprovalOut, EventOut, RunDetail, RunOut, Scenario, StepOut,
)

# Test seam: called as hook(point, step_name) at named crash windows.
Hook = Callable[[str, str], None]


class RunNotFound(Exception):
    pass


class OrderNotFound(Exception):
    pass


class Conflict(Exception):
    pass


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True)


def _event(conn, run_id: str, type_: str, payload: dict) -> None:
    """Append an event. Must be called inside tx()."""
    conn.execute(
        "INSERT INTO events(run_id, seq, type, payload_json, ts) VALUES "
        "(?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id=?), ?, ?, ?)",
        (run_id, run_id, type_, _dumps(payload), now()),
    )


def _set_status(conn, run_id: str, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET status=?, error=?, updated_at=? WHERE id=?", (status, error, now(), run_id)
    )


def _get_run(conn, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise RunNotFound(run_id)
    return row


def _load_state(conn, run) -> dict:
    outputs = {
        r["name"]: json.loads(r["output_json"])
        for r in conn.execute(
            "SELECT name, output_json FROM steps WHERE run_id=? AND status='succeeded' ORDER BY idx",
            (run["id"],),
        )
    }
    return {"order_id": run["order_id"], "scenario": json.loads(run["scenario_json"]), "outputs": outputs}


def start_run(conn, order_id: str, scenario: Scenario, hook: Hook | None = None) -> str:
    if not tools.order_exists(order_id):
        raise OrderNotFound(order_id)
    run_id = uuid.uuid4().hex
    with tx(conn):
        conn.execute(
            "INSERT INTO runs(id, order_id, scenario_json, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (run_id, order_id, _dumps(scenario.model_dump()), "running", now(), now()),
        )
        _event(conn, run_id, "run_created", {"order_id": order_id, "scenario": scenario.model_dump()})
    advance(conn, run_id, hook)
    return run_id


def advance(conn, run_id: str, hook: Hook | None = None) -> str:
    """Drive the run until it finishes, fails, or needs approval. Returns status."""
    while True:
        run = _get_run(conn, run_id)
        if run["status"] != "running":
            return run["status"]
        state = _load_state(conn, run)

        pending = conn.execute(
            "SELECT idx, name, idempotency_key FROM steps WHERE run_id=? AND status='started'", (run_id,)
        ).fetchone()
        if pending is not None:
            idx, name, key = pending["idx"], pending["name"], pending["idempotency_key"]
        else:
            name = planner.decide(state)
            if name is None:
                with tx(conn):
                    _set_status(conn, run_id, "succeeded")
                    _event(conn, run_id, "run_succeeded", {})
                return "succeeded"
            idx = conn.execute("SELECT COUNT(*) FROM steps WHERE run_id=?", (run_id,)).fetchone()[0]
            if _needs_approval(conn, run_id, idx, name, state):
                return "awaiting_approval"
            key = f"{run_id}:{idx}"
            with tx(conn):
                conn.execute(
                    "INSERT INTO steps(run_id, idx, name, status, input_json, idempotency_key, started_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (run_id, idx, name, "started", _dumps(state), key, now()),
                )
                _event(conn, run_id, "step_started", {"idx": idx, "name": name})

        try:
            output = tools.TOOLS[name](conn, key, state)
        except tools.ToolError as exc:
            with tx(conn):
                conn.execute(
                    "UPDATE steps SET status='failed', error=?, finished_at=? WHERE run_id=? AND idx=?",
                    (str(exc), now(), run_id, idx),
                )
                _set_status(conn, run_id, "failed", str(exc))
                _event(conn, run_id, "step_failed", {"idx": idx, "name": name, "error": str(exc)})
                _event(conn, run_id, "run_failed", {"error": str(exc)})
            return "failed"

        if hook:
            hook("after_tool", name)

        with tx(conn):
            # Conditional: a concurrent advance may already have completed this step.
            done = conn.execute(
                "UPDATE steps SET status='succeeded', output_json=?, finished_at=? "
                "WHERE run_id=? AND idx=? AND status='started'",
                (_dumps(output), now(), run_id, idx),
            ).rowcount
            if done:
                state["outputs"][name] = output
                conn.execute(
                    "INSERT INTO checkpoints(run_id, after_step_idx, state_json, ts) VALUES (?,?,?,?)",
                    (run_id, idx, _dumps(state), now()),
                )
                _event(conn, run_id, "step_succeeded", {"idx": idx, "name": name, "output": output})


def _needs_approval(conn, run_id: str, idx: int, name: str, state: dict) -> bool:
    """True if the run must pause before step idx. Creates the pending approval."""
    reason = planner.approval_reason(name, state)
    if reason is None:
        return False
    existing = conn.execute(
        "SELECT status FROM approvals WHERE run_id=? AND step_idx=?", (run_id, idx)
    ).fetchone()
    if existing is not None and existing["status"] == "approved":
        return False
    with tx(conn):
        if existing is None:
            conn.execute(
                "INSERT INTO approvals(run_id, step_idx, status, requested_payload_json) VALUES (?,?,?,?)",
                (run_id, idx, "pending", _dumps({
                    "step": name, "reason": reason,
                    "amount_paise": state["outputs"]["propose_refund"]["amount_paise"],
                })),
            )
            _event(conn, run_id, "approval_requested", {"step_idx": idx, "step": name, "reason": reason})
        _set_status(conn, run_id, "awaiting_approval")
    return True


def _decide(conn, run_id: str, decision: str, decided_by: str, reason: str | None) -> None:
    _get_run(conn, run_id)
    new_run_status = "running" if decision == "approved" else "rejected"
    with tx(conn):
        # Conditional transition: only one decider can move pending -> decided.
        changed = conn.execute(
            "UPDATE approvals SET status=?, decided_by=?, reason=?, decided_at=? "
            "WHERE run_id=? AND status='pending' "
            "AND EXISTS (SELECT 1 FROM runs WHERE id=? AND status='awaiting_approval')",
            (decision, decided_by, reason, now(), run_id, run_id),
        ).rowcount
        if not changed:
            raise Conflict(f"run {run_id} has no pending approval")
        _set_status(conn, run_id, new_run_status)
        _event(conn, run_id, f"approval_{decision}", {"decided_by": decided_by, "reason": reason})
        if decision == "rejected":
            _event(conn, run_id, "run_rejected", {})


def approve(conn, run_id: str, decided_by: str, hook: Hook | None = None) -> str:
    _decide(conn, run_id, "approved", decided_by, None)
    return advance(conn, run_id, hook)


def reject(conn, run_id: str, decided_by: str, reason: str | None = None) -> str:
    _decide(conn, run_id, "rejected", decided_by, reason)
    return "rejected"


# ---- reads ----

def _run_out(row) -> RunOut:
    return RunOut(
        id=row["id"], order_id=row["order_id"], status=row["status"],
        scenario=Scenario(**json.loads(row["scenario_json"])), error=row["error"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def get_run(conn, run_id: str) -> RunOut:
    return _run_out(_get_run(conn, run_id))


def list_runs(conn) -> list[RunOut]:
    return [_run_out(r) for r in conn.execute("SELECT * FROM runs ORDER BY created_at, rowid")]


def get_run_detail(conn, run_id: str) -> RunDetail:
    run = _get_run(conn, run_id)
    steps = [
        StepOut(
            idx=r["idx"], name=r["name"], status=r["status"], input=json.loads(r["input_json"]),
            output=json.loads(r["output_json"]) if r["output_json"] else None, error=r["error"],
            idempotency_key=r["idempotency_key"], started_at=r["started_at"], finished_at=r["finished_at"],
        )
        for r in conn.execute("SELECT * FROM steps WHERE run_id=? ORDER BY idx", (run_id,))
    ]
    appr = conn.execute(
        "SELECT * FROM approvals WHERE run_id=? AND status='pending'", (run_id,)
    ).fetchone()
    cp = conn.execute(
        "SELECT MAX(after_step_idx) FROM checkpoints WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    return RunDetail(
        **_run_out(run).model_dump(),
        steps=steps,
        pending_approval=ApprovalOut(
            step_idx=appr["step_idx"], status=appr["status"],
            requested_payload=json.loads(appr["requested_payload_json"]),
            decided_by=appr["decided_by"], reason=appr["reason"], decided_at=appr["decided_at"],
        ) if appr else None,
        latest_checkpoint_idx=cp,
    )


def list_events(conn, run_id: str) -> list[EventOut]:
    _get_run(conn, run_id)
    return [
        EventOut(seq=r["seq"], type=r["type"], payload=json.loads(r["payload_json"]), ts=r["ts"])
        for r in conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))
    ]
