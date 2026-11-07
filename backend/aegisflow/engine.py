"""Run engine. The only module that writes runs/steps/events/checkpoints/approvals.

Recovery model: advance() is re-entrant. State is rebuilt from succeeded steps;
a step left in 'started' (crash between tool call and completion) is resumed
with the same idempotency key rather than re-inserted.

Retries: a RetryableToolError keeps the step 'started' and bumps steps.attempt
(with a step_attempt_failed event) before a deterministic backoff, so every
attempt reuses the step's idempotency key. A crash mid-attempt re-executes the
same attempt number; a crash during backoff resumes at the next one. Crashes
therefore do not consume retry budget; only recorded tool failures do.

Interruption: an 'interrupt_after_commit' fault stops the run after the tool
call (for issue_refund, after the ledger commit) and before the step is
recorded, exactly like a crash at the after_tool hook, except that advance()
returns normally and records run_interrupted. The run stays 'running' until
resume() continues it with the same step and key.

Reconciliation: an error from a side-effecting call says nothing about whether
the effect happened: the response may have been lost, or the provider may be
wrong. So before re-executing such a step, and before reporting any error from
one, the engine asks the provider (tools.lookup). If the effect is there the step
is recorded without another call. "No record" is believed only while the key is
young enough that the provider can't have forgotten it (KEY_TRUST_S), and only as
grounds for retrying under the same key, never as proof of failure, since a
request may still be in flight. Whatever the lookup can't settle puts the run in
'needs_review', which no driver leaves on its own: a later lookup that finds the
effect (reconcile()) or a person (resolve()) has to settle it.

Concurrency: every advance() first takes the run's lease in a write transaction,
so normally one advance() drives a run at a time and another caller gets
Conflict. The lease is released when advance() returns or raises, and one left
behind by a killed process expires after LEASE_S.

Expiry alone can't guarantee exclusion: a driver that stalls past LEASE_S, or
whose lease a forward clock jump expires, doesn't know it has been replaced. So
each claim increments runs.lease_epoch, a fencing token, and every write a driver
makes first checks its epoch in the same transaction (_fenced_tx). Once a newer
driver has claimed the run, the old one's writes raise LeaseLost and change
nothing. The clock decides when a takeover may happen; only the epoch decides
whose writes count.
"""
import json
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import planner, runtime, tools
from .db import now, tx
from .models import (
    ApprovalOut,
    EventOut,
    RefundOut,
    RunDetail,
    RunOut,
    Scenario,
    StepOut,
)

# Test seam: called as hook(point, step_name) at named crash windows.
Hook = Callable[[str, str], None]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.1
    max_delay_s: float = 2.0

    def delay(self, attempt: int) -> float:
        """Backoff after failed attempt `attempt`. Exponential, capped, no jitter."""
        return min(self.base_delay_s * 2 ** (attempt - 1), self.max_delay_s)


RETRY_POLICY = RetryPolicy()
LEASE_S = 60
# How long a provider's "no record" can be believed. Must stay below its key retention.
KEY_TRUST_S = 300


class RunNotFound(Exception):
    pass


class OrderNotFound(Exception):
    pass


class Conflict(Exception):
    pass


class LeaseLost(Conflict):
    """A newer driver has claimed this run. Nothing was written."""


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


def _claim(conn, run_id: str) -> int:
    """Take the run's lease or raise Conflict, returning the new fencing epoch.
    Must be called inside tx()."""
    claimed_at = now()
    expires = (datetime.fromisoformat(claimed_at) + timedelta(seconds=LEASE_S)).isoformat()
    claimed = conn.execute(
        "UPDATE runs SET lease_epoch=lease_epoch+1, lease_token=lease_epoch+1, lease_expires_at=? "
        "WHERE id=? AND (lease_token IS NULL OR lease_expires_at < ?)",
        (expires, run_id, claimed_at),
    ).rowcount
    if not claimed:
        raise Conflict(f"run {run_id} is already being advanced")
    return conn.execute("SELECT lease_epoch FROM runs WHERE id=?", (run_id,)).fetchone()[0]


@contextmanager
def _fenced_tx(conn, run_id: str, epoch: int):
    """A write transaction that commits only if `epoch` still owns the run. The
    check and the writes are atomic, so a driver can't be replaced between them."""
    with tx(conn):
        current = conn.execute("SELECT lease_epoch FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        if current != epoch:
            raise LeaseLost(f"run {run_id} was taken over: this driver holds epoch {epoch}, the run is at {current}")
        yield


def _release(conn, run_id: str, epoch: int) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE runs SET lease_token=NULL, lease_expires_at=NULL WHERE id=? AND lease_epoch=?",
            (run_id, epoch),
        )


def _drive(conn, run_id: str, epoch: int, hook: Hook | None) -> str:
    """Advance under a lease the caller took, releasing it however advancing ends."""
    try:
        return _advance(conn, run_id, epoch, hook)
    finally:
        _release(conn, run_id, epoch)


def run_id_for_key(conn, idempotency_key: str) -> str | None:
    row = conn.execute("SELECT id FROM runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    return row["id"] if row else None


def start_run_once(conn, order_id: str, scenario: Scenario, idempotency_key: str | None = None,
                   hook: Hook | None = None) -> tuple[str, bool]:
    """Create and drive a run. Returns (run id, whether this call created it): a
    repeated request with the same key gets the original run back as it stands,
    without driving it. Reusing a key for a different request is a Conflict."""
    if not tools.order_exists(order_id):
        raise OrderNotFound(order_id)
    run_id = runtime.current.new_id()
    scenario_json = _dumps(scenario.model_dump())
    with tx(conn):
        if idempotency_key is not None:
            existing = conn.execute(
                "SELECT id, order_id, scenario_json FROM runs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if (existing["order_id"], existing["scenario_json"]) != (order_id, scenario_json):
                    raise Conflict(f"idempotency key {idempotency_key} was already used for a different request")
                return existing["id"], False
        conn.execute(
            "INSERT INTO runs(id, order_id, scenario_json, status, created_at, updated_at, idempotency_key) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, order_id, scenario_json, "running", now(), now(), idempotency_key),
        )
        _event(conn, run_id, "run_created", {"order_id": order_id, "scenario": scenario.model_dump()})
    advance(conn, run_id, hook)
    return run_id, True


def start_run(conn, order_id: str, scenario: Scenario, hook: Hook | None = None,
              idempotency_key: str | None = None) -> str:
    return start_run_once(conn, order_id, scenario, idempotency_key, hook)[0]


def advance(conn, run_id: str, hook: Hook | None = None) -> str:
    """Drive the run until it finishes, fails, needs approval or is interrupted.
    Returns status. Raises Conflict if another advance() holds the run."""
    with tx(conn):
        _get_run(conn, run_id)
        epoch = _claim(conn, run_id)
    return _drive(conn, run_id, epoch, hook)


def resume(conn, run_id: str, hook: Hook | None = None) -> str:
    """Explicitly continue a run left 'running' by an interruption or crash."""
    with tx(conn):
        run = _get_run(conn, run_id)
        if run["status"] != "running":
            raise Conflict(f"run {run_id} is {run['status']}; only a running run can be resumed")
        epoch = _claim(conn, run_id)
        pending = conn.execute(
            "SELECT idx, name, idempotency_key FROM steps WHERE run_id=? AND status='started'", (run_id,)
        ).fetchone()
        _event(conn, run_id, "run_resumed", dict(pending) if pending else {})
    return _drive(conn, run_id, epoch, hook)


def _wait_for_backoff(next_attempt_at: str | None) -> None:
    """Whoever picks up a step mid-backoff waits out what's left of it. Capped
    because the clock may have jumped back since the delay was scheduled."""
    if next_attempt_at is not None:
        remaining = (datetime.fromisoformat(next_attempt_at) - datetime.fromisoformat(now())).total_seconds()
        if remaining > 0:
            runtime.current.sleep(min(remaining, RETRY_POLICY.max_delay_s))


def _advance(conn, run_id: str, epoch: int, hook: Hook | None) -> str:
    waited_for = None  # The backoff this driver has already slept through itself.
    while True:
        run = _get_run(conn, run_id)
        if run["lease_epoch"] != epoch:
            raise LeaseLost(f"run {run_id} was taken over from epoch {epoch}")  # Don't start work for nothing.
        if run["status"] != "running":
            return run["status"]
        state = _load_state(conn, run)

        pending = conn.execute(
            "SELECT idx, name, idempotency_key, attempt, next_attempt_at FROM steps "
            "WHERE run_id=? AND status='started'",
            (run_id,),
        ).fetchone()
        if pending is not None:
            idx, name, key = pending["idx"], pending["name"], pending["idempotency_key"]
            attempt = pending["attempt"]
            if pending["next_attempt_at"] != waited_for:
                _wait_for_backoff(pending["next_attempt_at"])
            if name in tools.SIDE_EFFECTING:
                view = _ask_provider(conn, run_id, idx, name, key, state)
                if view.output is not None:
                    with _fenced_tx(conn, run_id, epoch):
                        _event(conn, run_id, "step_reconciled", {"idx": idx, "name": name, "idempotency_key": key})
                        _write_success(conn, run_id, idx, name, attempt, view.output, state)
                    continue
                if view.doubt is not None:
                    return _hold_for_review(conn, run_id, epoch, idx, name, key, view.doubt)
        else:
            name = planner.decide(state)
            if name is None:
                with _fenced_tx(conn, run_id, epoch):
                    _set_status(conn, run_id, "succeeded")
                    _event(conn, run_id, "run_succeeded", {})
                return "succeeded"
            idx = conn.execute("SELECT COUNT(*) FROM steps WHERE run_id=?", (run_id,)).fetchone()[0]
            if _needs_approval(conn, run_id, epoch, idx, name, state):
                return "awaiting_approval"
            key = f"{run_id}:{idx}"
            attempt = 1
            with _fenced_tx(conn, run_id, epoch):
                conn.execute(
                    "INSERT INTO steps(run_id, idx, name, status, input_json, attempt, idempotency_key, "
                    "started_at) VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, idx, name, "started", _dumps(state), attempt, key, now()),
                )
                _event(conn, run_id, "step_started", {"idx": idx, "name": name})

        try:
            output = tools.call(conn, name, key, state, attempt)
        except tools.RetryableToolError as exc:
            if attempt < RETRY_POLICY.max_attempts:
                delay = RETRY_POLICY.delay(attempt)
                waited_for = (datetime.fromisoformat(now()) + timedelta(seconds=delay)).isoformat()
                with _fenced_tx(conn, run_id, epoch):
                    conn.execute(
                        "UPDATE steps SET attempt=attempt+1, error=?, next_attempt_at=? WHERE run_id=? AND idx=?",
                        (str(exc), waited_for, run_id, idx),
                    )
                    _event(conn, run_id, "step_attempt_failed", {
                        "idx": idx, "name": name, "attempt": attempt, "error": str(exc),
                        "retryable": True, "next_delay_s": delay,
                    })
                if hook:
                    hook("retry_scheduled", name)
                runtime.current.sleep(delay)  # Outside tx(): never hold the write lock while waiting.
                continue
            error = f"retries exhausted after {attempt} attempts: {exc}"
            info: dict = {"attempt": attempt, "retryable": True, "exhausted": True}
            if name not in tools.SIDE_EFFECTING:
                return _fail(conn, run_id, epoch, idx, name, error, info)
            ended = _after_failed_call(conn, run_id, epoch, idx, name, key, attempt, state, error, info, True)
            if ended is None:
                continue
            return ended
        except tools.ToolError as exc:
            info = {"attempt": attempt, "retryable": False}
            if name not in tools.SIDE_EFFECTING:
                return _fail(conn, run_id, epoch, idx, name, str(exc), info)
            # On a step's first execution a refusal with nothing at the provider is a known outcome.
            in_flight = pending is not None
            ended = _after_failed_call(conn, run_id, epoch, idx, name, key, attempt, state, str(exc), info, in_flight)
            if ended is None:
                continue
            return ended

        if hook:
            hook("after_tool", name)
        if _interrupt_planned(conn, run_id, idx, name, attempt, state):
            with _fenced_tx(conn, run_id, epoch):
                _event(conn, run_id, "run_interrupted", {
                    "idx": idx, "name": name, "attempt": attempt, "idempotency_key": key, "simulated": True,
                })
            return "running"

        with _fenced_tx(conn, run_id, epoch):
            _write_success(conn, run_id, idx, name, attempt, output, state)


def _write_success(conn, run_id: str, idx: int, name: str, attempt: int, output: dict, state: dict) -> None:
    """Record a step's result. Must be called inside a fenced transaction."""
    conn.execute(
        "UPDATE steps SET status='succeeded', output_json=?, finished_at=? WHERE run_id=? AND idx=?",
        (_dumps(output), now(), run_id, idx),
    )
    state["outputs"][name] = output
    conn.execute(
        "INSERT INTO checkpoints(run_id, after_step_idx, state_json, ts) VALUES (?,?,?,?)",
        (run_id, idx, _dumps(state), now()),
    )
    _event(conn, run_id, "step_succeeded", {"idx": idx, "name": name, "attempt": attempt, "output": output})


@dataclass(frozen=True)
class ProviderView:
    """What a status lookup established. With an output, the effect happened; with
    a doubt, nobody can tell. With neither, the provider has no record and can be
    trusted to deduplicate a retry under the same key."""
    output: dict | None = None
    doubt: str | None = None


def _ask_provider(conn, run_id: str, idx: int, name: str, key: str, state: dict) -> ProviderView:
    try:
        output = tools.lookup(conn, name, key, state)
    except (tools.RetryableToolError, tools.ToolError) as exc:
        return ProviderView(doubt=f"status lookup failed: {exc}")
    if output is not None:
        return ProviderView(output=output)
    started_at = conn.execute("SELECT started_at FROM steps WHERE run_id=? AND idx=?", (run_id, idx)).fetchone()[0]
    age = (datetime.fromisoformat(now()) - datetime.fromisoformat(started_at)).total_seconds()
    if age >= KEY_TRUST_S:
        return ProviderView(doubt=f"provider has no record of {key}, but it is {int(age)} s old and may be forgotten")
    return ProviderView()


def _after_failed_call(conn, run_id: str, epoch: int, idx: int, name: str, key: str, attempt: int, state: dict,
                       error: str, info: dict, in_flight: bool) -> str | None:
    """A side-effecting call failed for good. Returns how the run ended, or None if
    the effect happened after all and the step is now recorded as a success."""
    view = _ask_provider(conn, run_id, idx, name, key, state)
    if view.output is not None:
        with _fenced_tx(conn, run_id, epoch):
            _event(conn, run_id, "step_reconciled", {
                "idx": idx, "name": name, "idempotency_key": key, "reported_error": error,
            })
            _write_success(conn, run_id, idx, name, attempt, view.output, state)
        return None
    if view.doubt is not None:
        return _hold_for_review(conn, run_id, epoch, idx, name, key, f"{error}; {view.doubt}")
    if in_flight:
        return _hold_for_review(conn, run_id, epoch, idx, name, key,
                                f"{error}; provider has no record, but an earlier request may still arrive")
    return _fail(conn, run_id, epoch, idx, name, error, info)


def _hold_for_review(conn, run_id: str, epoch: int, idx: int, name: str, key: str, reason: str) -> str:
    """The outcome of a side-effecting step can't be established. Say so, and stop."""
    with _fenced_tx(conn, run_id, epoch):
        _set_status(conn, run_id, "needs_review", reason)
        _event(conn, run_id, "run_needs_review", {
            "idx": idx, "name": name, "idempotency_key": key, "outcome_unknown": True, "reason": reason,
        })
    return "needs_review"


def _fail(conn, run_id: str, epoch: int, idx: int, name: str, error: str, info: dict) -> str:
    """Terminal step failure: fails the step and the run; no later step runs.
    Failing doesn't undo earlier steps, so name any effects that already happened."""
    with _fenced_tx(conn, run_id, epoch):
        committed = [
            r["idempotency_key"] for r in conn.execute(
                "SELECT name, idempotency_key FROM steps WHERE run_id=? AND status='succeeded' ORDER BY idx", (run_id,)
            ) if r["name"] in tools.SIDE_EFFECTING
        ]
        if committed:
            info = {**info, "effects_committed": committed}
        conn.execute(
            "UPDATE steps SET status='failed', error=?, finished_at=? WHERE run_id=? AND idx=?",
            (error, now(), run_id, idx),
        )
        _set_status(conn, run_id, "failed", error)
        _event(conn, run_id, "step_failed", {"idx": idx, "name": name, "error": error, **info})
        _event(conn, run_id, "run_failed", {"error": error, **info})
    return "failed"


def _interrupt_planned(conn, run_id: str, idx: int, name: str, attempt: int, state: dict) -> bool:
    """True if the fault plan interrupts this attempt and has not done so yet.
    Resuming re-executes the same attempt, so the interruption fires only once."""
    plan = state["scenario"].get("faults", {}).get(name, [])
    if attempt > len(plan) or plan[attempt - 1] != "interrupt_after_commit":
        return False
    return conn.execute(
        "SELECT 1 FROM events WHERE run_id=? AND type='run_interrupted' AND json_extract(payload_json, '$.idx')=?",
        (run_id, idx),
    ).fetchone() is None


def _needs_approval(conn, run_id: str, epoch: int, idx: int, name: str, state: dict) -> bool:
    """True if the run must pause before step idx. Creates the pending approval."""
    reason = planner.approval_reason(name, state)
    if reason is None:
        return False
    existing = conn.execute(
        "SELECT status FROM approvals WHERE run_id=? AND step_idx=?", (run_id, idx)
    ).fetchone()
    if existing is not None and existing["status"] == "approved":
        return False
    with _fenced_tx(conn, run_id, epoch):
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


def _decide(conn, run_id: str, decision: str, decided_by: str, reason: str | None) -> int | None:
    """Record the decision. On approval, also takes the run's lease, in the same
    transaction, and returns its epoch for the caller to advance under."""
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
            return None
        return _claim(conn, run_id)


def approve(conn, run_id: str, decided_by: str, hook: Hook | None = None) -> str:
    epoch = _decide(conn, run_id, "approved", decided_by, None)
    assert epoch is not None
    return _drive(conn, run_id, epoch, hook)


def reject(conn, run_id: str, decided_by: str, reason: str | None = None) -> str:
    _decide(conn, run_id, "rejected", decided_by, reason)
    return "rejected"


def _claim_review(conn, run_id: str):
    """Take the lease on a run held for review. Returns (run, held step, epoch)."""
    with tx(conn):
        run = _get_run(conn, run_id)
        if run["status"] != "needs_review":
            raise Conflict(f"run {run_id} is {run['status']}; only a run held for review can be settled")
        epoch = _claim(conn, run_id)
    step = conn.execute(
        "SELECT idx, name, idempotency_key, attempt FROM steps WHERE run_id=? AND status='started'", (run_id,)
    ).fetchone()
    return run, step, epoch


def _clear_review(conn, run_id: str, epoch: int, step, output: dict, state: dict, by: str) -> None:
    with _fenced_tx(conn, run_id, epoch):
        _set_status(conn, run_id, "running")
        _event(conn, run_id, "run_review_cleared", {
            "idx": step["idx"], "name": step["name"], "idempotency_key": step["idempotency_key"], "by": by,
        })
        _write_success(conn, run_id, step["idx"], step["name"], step["attempt"], output, state)


def reconcile(conn, run_id: str, hook: Hook | None = None) -> str:
    """Ask the provider again about a run held for review. If the effect is there,
    the run carries on; otherwise it stays held, because no record proves nothing."""
    run, step, epoch = _claim_review(conn, run_id)
    try:
        state = _load_state(conn, run)
        view = _ask_provider(conn, run_id, step["idx"], step["name"], step["idempotency_key"], state)
        if view.output is None:
            return "needs_review"
        _clear_review(conn, run_id, epoch, step, view.output, state, "provider")
        return _advance(conn, run_id, epoch, hook)
    finally:
        _release(conn, run_id, epoch)


def resolve(conn, run_id: str, decided_by: str, outcome: str, hook: Hook | None = None) -> str:
    """A person settles what the provider can't. They may say the effect happened
    when the provider has forgotten it, but can't deny one the provider shows."""
    run, step, epoch = _claim_review(conn, run_id)
    try:
        state = _load_state(conn, run)
        idx, name, key = step["idx"], step["name"], step["idempotency_key"]
        view = _ask_provider(conn, run_id, idx, name, key, state)
        if outcome == "not_applied":
            if view.output is not None:
                raise Conflict(f"run {run_id}: the provider shows this effect under {key}")
            error = f"{decided_by} confirmed that {name} did not happen"
            return _fail(conn, run_id, epoch, idx, name, error, {"resolved_by": decided_by})
        output = view.output or tools.EXPECTED_OUTPUT[name](key, state)
        _clear_review(conn, run_id, epoch, step, output, state, decided_by)
        return _advance(conn, run_id, epoch, hook)
    finally:
        _release(conn, run_id, epoch)


# ---- reads ----

# Runs plus a derived 'interrupted' flag: still running, and the latest
# interrupt/resume event is an interruption, so nothing has resumed it since.
_RUNS_READ = """
SELECT runs.*, (
    status = 'running' AND (
        SELECT type FROM events
        WHERE run_id = runs.id AND type IN ('run_interrupted', 'run_resumed')
        ORDER BY seq DESC LIMIT 1
    ) = 'run_interrupted'
) AS interrupted
FROM runs
"""


def _read_run(conn, run_id: str) -> sqlite3.Row:
    row = conn.execute(_RUNS_READ + "WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise RunNotFound(run_id)
    return row


def _run_out(row) -> RunOut:
    return RunOut(
        id=row["id"], order_id=row["order_id"], status=row["status"], interrupted=bool(row["interrupted"]),
        scenario=Scenario(**json.loads(row["scenario_json"])), error=row["error"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def get_run(conn, run_id: str) -> RunOut:
    return _run_out(_read_run(conn, run_id))


def count_runs(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def list_runs(conn) -> list[RunOut]:
    return [_run_out(r) for r in conn.execute(_RUNS_READ + "ORDER BY created_at, rowid")]


def get_run_detail(conn, run_id: str) -> RunDetail:
    run = _read_run(conn, run_id)
    steps = [
        StepOut(
            idx=r["idx"], name=r["name"], status=r["status"], input=json.loads(r["input_json"]),
            output=json.loads(r["output_json"]) if r["output_json"] else None, error=r["error"],
            attempt=r["attempt"], idempotency_key=r["idempotency_key"], started_at=r["started_at"],
            finished_at=r["finished_at"],
        )
        for r in conn.execute("SELECT * FROM steps WHERE run_id=? ORDER BY idx", (run_id,))
    ]
    appr = conn.execute(
        "SELECT * FROM approvals WHERE run_id=? AND status='pending'", (run_id,)
    ).fetchone()
    cp = conn.execute(
        "SELECT MAX(after_step_idx) FROM checkpoints WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    refunds = [
        RefundOut(**dict(r))
        for r in conn.execute(
            "SELECT r.idempotency_key, r.order_id, r.amount_paise, r.created_at FROM refunds r "
            "JOIN steps s ON s.idempotency_key = r.idempotency_key WHERE s.run_id=? ORDER BY s.idx",
            (run_id,),
        )
    ]
    return RunDetail(
        **_run_out(run).model_dump(),
        steps=steps,
        pending_approval=ApprovalOut(
            step_idx=appr["step_idx"], status=appr["status"],
            requested_payload=json.loads(appr["requested_payload_json"]),
            decided_by=appr["decided_by"], reason=appr["reason"], decided_at=appr["decided_at"],
        ) if appr else None,
        latest_checkpoint_idx=cp,
        refunds=refunds,
    )


def list_events(conn, run_id: str) -> list[EventOut]:
    _get_run(conn, run_id)
    return [
        EventOut(seq=r["seq"], type=r["type"], payload=json.loads(r["payload_json"]), ts=r["ts"])
        for r in conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))
    ]
