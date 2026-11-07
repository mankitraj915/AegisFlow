"""The history checker: AegisFlow's claims as properties of what actually happened.

It watches every commit, tool call and client reply during a run, then examines
the final state. It reads the database and the external system directly and
shares no logic with the engine, so an engine bug can't hide by agreeing with itself.
"""
import json
from collections import Counter, defaultdict

from .. import engine, planner, tools
from .scheduler import Proc
from .storage import WRITE_VERBS

MAX_ATTEMPTS = engine.RETRY_POLICY.max_attempts
POLICY = engine.RetryPolicy()
TERMINAL = {"succeeded", "failed", "rejected"}
LEGAL_RUN_MOVES = {
    "running": {"awaiting_approval", "succeeded", "failed"},
    "awaiting_approval": {"running", "rejected"},
}
LEGAL_STEP_MOVES = {"started": {"succeeded", "failed"}}
RUN_RANK = {"running": 0, "awaiting_approval": 0, **{s: 1 for s in TERMINAL}}

# Every invariant, so a survey can report the ones that never fired.
INVARIANTS = (
    "refund_cap",
    "effect_duplicated",
    "notified_twice",
    "terminal_changed",
    "illegal_transition",
    "step_regressed",
    "stale_write",
    "rerun_after_success",
    "retry_limit",
    "wrong_backoff_delay",
    "backoff_skipped",
    "history_mismatch",
    "checkpoint_mismatch",
    "ack_lost",
    "orphan_effect",
    "succeeded_without_effect",
    "succeeded_without_notify",
    "effect_on_rejected",
    "gate_bypassed",
    "double_decision",
    "undisclosed_effect_on_failed_step",
    "undisclosed_effect_before_failure",
    "request_ran_twice",
    "read_path_writes",
    "money_not_integer",
    "stuck_run",
    "integrity_error",
    "unexpected_exception",
    "runaway",
)


class Checker:
    def __init__(self, world):
        self.world = world
        self.violations: list[tuple[str, str]] = []
        self.seen: Counter = Counter()
        self._reset_snapshot()
        self.claim_count: Counter = Counter()
        self.failed_attempts: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
        self.succeeded_at: dict[str, int] = {}
        self.forward_jumps: list[int] = []
        self.last_skew = 0
        self.acks: dict[str, str] = {}
        self.requests: dict[str, set[str]] = defaultdict(set)

    def _reset_snapshot(self) -> None:
        self.runs: dict[str, dict] = {}
        self.steps: dict[tuple[str, int], dict] = {}
        self.last_event_id = 0

    def fail(self, invariant: str, detail: str) -> None:
        self.seen[invariant] += 1
        if self.seen[invariant] == 1:
            self.violations.append((invariant, detail))
            self.world.sched.log(f"violation {invariant}: {detail}")

    # ---- during the run ----

    def clock_jumped(self, skew_us: int) -> None:
        if skew_us > self.last_skew:
            self.forward_jumps.append(self.world.sched.true_us)
        self.last_skew = skew_us

    def power_restored(self) -> None:
        self._reset_snapshot()
        self.committed(None)

    def statement(self, proc: Proc, verb: str) -> None:
        if proc.readonly and verb in WRITE_VERBS:
            self.fail("read_path_writes", f"{proc.name} ran {verb} on a read-only path")

    def external_changed(self) -> None:
        rows = self.world.external.execute(
            "SELECT order_id, SUM(amount_paise) AS total FROM refunds GROUP BY order_id"
        ).fetchall()
        for row in rows:
            limit = tools.ORDERS[row["order_id"]]["total_paise"]
            if row["total"] > limit:
                self.fail("refund_cap", f"{row['order_id']}: refunded {row['total']} of {limit} paise")

    def committed(self, proc: Proc | None) -> None:
        keeper = self.world.keeper
        tables = {r["name"] for r in keeper.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"runs", "steps", "events"} <= tables:
            return
        runs = {r["id"]: dict(r) for r in keeper.execute("SELECT id, status, lease_token FROM runs")}
        for run_id, run in runs.items():
            old = self.runs.get(run_id)
            if run["lease_token"] and (old is None or old["lease_token"] != run["lease_token"]):
                self.claim_count[run_id] += 1
                if proc is not None:
                    proc.claims[run_id] = self.claim_count[run_id]
            if old is not None and old["status"] != run["status"]:
                if old["status"] in TERMINAL:
                    self.fail("terminal_changed", f"{run_id[:8]}: {old['status']} became {run['status']}")
                elif run["status"] not in LEGAL_RUN_MOVES[old["status"]]:
                    self.fail("illegal_transition", f"{run_id[:8]}: {old['status']} became {run['status']}")
        self.runs = runs

        steps = {
            (r["run_id"], r["idx"]): dict(r)
            for r in keeper.execute("SELECT run_id, idx, status, attempt FROM steps")
        }
        for key, step in steps.items():
            old_step = self.steps.get(key)
            if old_step is None:
                continue
            if old_step["status"] != step["status"] and step["status"] not in LEGAL_STEP_MOVES.get(
                old_step["status"], ()
            ):
                self.fail("step_regressed", f"step {key[1]}: {old_step['status']} became {step['status']}")
            if step["attempt"] < old_step["attempt"]:
                self.fail("step_regressed", f"step {key[1]}: attempt fell to {step['attempt']}")
            if step["attempt"] > MAX_ATTEMPTS:
                self.fail("retry_limit", f"step {key[1]}: attempt {step['attempt']}")
        self.steps = steps

        now = self.world.sched.true_us
        for event in keeper.execute(
            "SELECT id, run_id, type, payload_json FROM events WHERE id>? ORDER BY id", (self.last_event_id,)
        ).fetchall():
            self.last_event_id = event["id"]
            run_id = event["run_id"]
            if proc is not None and run_id in proc.claims and proc.claims[run_id] != self.claim_count[run_id]:
                self.fail("stale_write", f"{proc.name} wrote {event['type']} after losing {run_id[:8]}")
            payload = json.loads(event["payload_json"])
            if "idx" in payload:
                step_key = f"{run_id}:{payload['idx']}"
                if event["type"] == "step_succeeded":
                    self.succeeded_at[step_key] = now
                elif event["type"] == "step_attempt_failed":
                    delay_us = int(payload["next_delay_s"] * 1_000_000)
                    self.failed_attempts[step_key].append((now, delay_us, payload["attempt"]))
                    if payload["next_delay_s"] != POLICY.delay(payload["attempt"]):
                        self.fail("wrong_backoff_delay", f"attempt {payload['attempt']}: {payload['next_delay_s']}")

    def tool_started(self, proc: Proc, name: str, key: str) -> None:
        now = self.world.sched.true_us
        for failed_at, delay_us, attempt in self.failed_attempts.get(key, ()):
            jumped = any(failed_at <= t <= now for t in self.forward_jumps)
            if now - failed_at < delay_us and not jumped:
                self.fail("backoff_skipped", f"{name}: retried {now - failed_at} us after attempt {attempt}, "
                          f"wanted {delay_us} us")
        succeeded_at = self.succeeded_at.get(key)
        if succeeded_at is not None and proc.started_us is not None and proc.started_us > succeeded_at:
            self.fail("rerun_after_success", f"{name} ran again in {proc.name} after it succeeded")

    def replied(self, proc: Proc, kind: str, outcome: str, run_id: str | None) -> None:
        if run_id is not None and outcome in RUN_RANK:
            if RUN_RANK[outcome] >= RUN_RANK.get(self.acks.get(run_id, "running"), 0):
                self.acks[run_id] = outcome

    def request_replied(self, request_id: str, run_id: str) -> None:
        self.requests[request_id].add(run_id)
        if len(self.requests[request_id]) > 1:
            self.fail("request_ran_twice", f"{request_id} ran as {len(self.requests[request_id])} runs")

    def server_error(self, proc: Proc, kind: str, exc: Exception) -> None:
        if "IntegrityError" in type(exc).__name__:
            self.fail("integrity_error", f"{proc.name}: {exc}")

    # ---- after the run ----

    def finish(self) -> None:
        world = self.world
        for run_id in world.unfinished():
            self.fail("stuck_run", f"{run_id[:8]} never finished")
        for proc in world.sched.procs:
            if proc.error:
                self.fail("unexpected_exception", f"{proc.name}: {proc.error.strip().splitlines()[-1]}")
        if world.sched.runaway:
            self.fail("runaway", "the run never stopped")
        for key, count in world.deliveries.items():
            if count > 1:
                self.fail("notified_twice", f"{key} delivered {count} times")

        keeper = world.keeper
        runs = {r["id"]: dict(r) for r in keeper.execute("SELECT * FROM runs")}
        refunds = [dict(r) for r in world.external.execute("SELECT * FROM refunds")]
        by_run: dict[str, list[dict]] = defaultdict(list)
        for row in refunds:
            run_id = row["idempotency_key"].split(":")[0]
            by_run[run_id].append(row)
            if type(row["amount_paise"]) is not int:
                self.fail("money_not_integer", f"refund of {row['amount_paise']!r}")
            if run_id not in runs:
                self.fail("orphan_effect", f"refund under {row['idempotency_key'][:8]} has no run")

        for run_id, status in self.acks.items():
            run = runs.get(run_id)
            if run is None:
                self.fail("ack_lost", f"{run_id[:8]} was acknowledged {status} and is gone")
            elif RUN_RANK[run["status"]] < RUN_RANK[status] or (status in TERMINAL and run["status"] != status):
                self.fail("ack_lost", f"{run_id[:8]} was acknowledged {status}, is now {run['status']}")

        for run_id, run in runs.items():
            self._check_run(run_id, run, by_run.get(run_id, []))

    def _check_run(self, run_id: str, run: dict, refunds: list[dict]) -> None:
        keeper = self.world.keeper
        events = [
            (e["seq"], e["type"], json.loads(e["payload_json"]))
            for e in keeper.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))
        ]
        steps = {s["idx"]: dict(s) for s in keeper.execute("SELECT * FROM steps WHERE run_id=?", (run_id,))}
        self._check_history(run_id, run, steps, events)
        self._check_checkpoints(run_id, steps)

        types = [t for _, t, _ in events]
        scenario = json.loads(run["scenario_json"])
        short = run_id[:8]
        if len(refunds) > 1:
            self.fail("effect_duplicated", f"{short}: {len(refunds)} refunds")
        if types.count("approval_approved") + types.count("approval_rejected") > 1:
            self.fail("double_decision", f"{short}: decided more than once")

        if run["status"] == "succeeded":
            refund_step = next((s for s in steps.values() if s["name"] == "issue_refund"), None)
            if not refunds or refund_step is None:
                self.fail("succeeded_without_effect", f"{short} succeeded with no refund")
            else:
                proposed = json.loads(refund_step["output_json"])["amount_paise"]
                if refunds[0]["amount_paise"] != proposed:
                    self.fail("succeeded_without_effect", f"{short} recorded {proposed}, ledger has "
                              f"{refunds[0]['amount_paise']}")
            notify = next((s for s in steps.values() if s["name"] == "notify_customer"), None)
            if notify is None or not self.world.deliveries[notify["idempotency_key"]]:
                self.fail("succeeded_without_notify", f"{short} succeeded with no message delivered")
        if run["status"] == "rejected" and refunds:
            self.fail("effect_on_rejected", f"{short} was rejected and has a refund")
        if refunds and "approval_approved" not in types:
            order = tools.ORDERS[run["order_id"]]
            state = {
                "scenario": scenario,
                "outputs": {"propose_refund": {"amount_paise": refunds[0]["amount_paise"]}, "fetch_order": order},
            }
            if planner.approval_reason("issue_refund", state):
                self.fail("gate_bypassed", f"{short} refunded with no approval")

        if run["status"] == "failed" and refunds:
            failure: dict = next((p for _, t, p in events if t == "run_failed"), {})
            failed_step = next((s for s in steps.values() if s["status"] == "failed"), None)
            refund_failed = failed_step is not None and failed_step["name"] == "issue_refund"
            if refund_failed and not failure.get("outcome_unknown"):
                self.fail("undisclosed_effect_on_failed_step", f"{short} failed at issue_refund with a refund "
                          "in the ledger and no outcome_unknown")
            elif not refund_failed and not failure.get("effects_committed"):
                self.fail("undisclosed_effect_before_failure", f"{short} failed after its refund committed "
                          "without saying so")

    def _check_history(self, run_id: str, run: dict, steps: dict, events: list) -> None:
        """Rebuild the run from its events alone and compare with the tables."""
        short = run_id[:8]
        if [seq for seq, _, _ in events] != list(range(1, len(events) + 1)):
            self.fail("history_mismatch", f"{short}: event sequence has gaps")
        status = "running"
        rebuilt: dict[int, dict] = {}
        terminal_events = 0
        for _, type_, payload in events:
            idx = payload.get("idx")
            if type_ == "approval_requested":
                status = "awaiting_approval"
            elif type_ == "approval_approved":
                status = "running"
            elif type_ == "run_rejected":
                status, terminal_events = "rejected", terminal_events + 1
            elif type_ == "run_succeeded":
                status, terminal_events = "succeeded", terminal_events + 1
            elif type_ == "run_failed":
                status, terminal_events = "failed", terminal_events + 1
            elif type_ == "step_started":
                rebuilt[idx] = {"status": "started", "attempt": 1, "output": None}
            elif type_ == "step_attempt_failed" and idx in rebuilt:
                rebuilt[idx]["attempt"] = payload["attempt"] + 1
            elif type_ == "step_succeeded" and idx in rebuilt:
                rebuilt[idx].update(status="succeeded", output=payload["output"])
            elif type_ == "step_failed" and idx in rebuilt:
                rebuilt[idx]["status"] = "failed"
        if terminal_events > 1:
            self.fail("history_mismatch", f"{short}: {terminal_events} terminal events")
        if status != run["status"]:
            self.fail("history_mismatch", f"{short}: events say {status}, table says {run['status']}")
        if set(rebuilt) != set(steps):
            self.fail("history_mismatch", f"{short}: events have steps {sorted(rebuilt)}, table {sorted(steps)}")
            return
        for idx, step in steps.items():
            want = rebuilt[idx]
            output = json.loads(step["output_json"]) if step["output_json"] else None
            actual = {"status": step["status"], "attempt": step["attempt"], "output": output}
            if actual != want:
                self.fail("history_mismatch", f"{short} step {idx}: events say {want}, table says {actual}")

    def _check_checkpoints(self, run_id: str, steps: dict) -> None:
        keeper = self.world.keeper
        checkpoints = {
            c["after_step_idx"]: json.loads(c["state_json"])
            for c in keeper.execute("SELECT * FROM checkpoints WHERE run_id=?", (run_id,))
        }
        succeeded = {idx for idx, s in steps.items() if s["status"] == "succeeded"}
        if set(checkpoints) != succeeded:
            self.fail("checkpoint_mismatch", f"{run_id[:8]}: checkpoints {sorted(checkpoints)}, "
                      f"succeeded {sorted(succeeded)}")
            return
        for idx, state in checkpoints.items():
            want = {s["name"]: json.loads(s["output_json"]) for i, s in steps.items() if i <= idx}
            if state["outputs"] != want:
                self.fail("checkpoint_mismatch", f"{run_id[:8]} checkpoint {idx} disagrees with its steps")
