"""Negative controls: put a known bug back and see whether the simulator notices.

A checker that never fails proves nothing, so each control here is an exact edit
to the real source, applied in memory for the length of a `with` block. An edit
whose target text has changed raises instead of quietly testing nothing.
"""
import ast
import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Mutation:
    name: str
    what: str
    catches: tuple[str, ...]  # Any of these counts as caught for the right reason.
    edits: tuple[tuple[str, str, str], ...]  # (module, old text, new text)


_RECORD_FIRST = """        if name == "issue_refund":
            output = {"refund_id": key, "order_id": state["order_id"],
                      "amount_paise": state["outputs"]["propose_refund"]["amount_paise"]}
            with _fenced_tx(conn, run_id, epoch):
                conn.execute(
                    "UPDATE steps SET status='succeeded', output_json=?, finished_at=? WHERE run_id=? AND idx=?",
                    (_dumps(output), now(), run_id, idx),
                )
                state["outputs"][name] = output
                conn.execute(
                    "INSERT INTO checkpoints(run_id, after_step_idx, state_json, ts) VALUES (?,?,?,?)",
                    (run_id, idx, _dumps(state), now()),
                )
                _event(conn, run_id, "step_succeeded", {
                    "idx": idx, "name": name, "attempt": attempt, "output": output,
                })
            try:
                tools.call(conn, name, key, state, attempt)
            except (tools.RetryableToolError, tools.ToolError):
                pass
            continue

        try:
            output = tools.call(conn, name, key, state, attempt)
"""

_IGNORE_REFUND_KEY = (
    ("aegisflow.tools", "FROM refunds WHERE idempotency_key=?", "FROM refunds WHERE 0 AND idempotency_key=?"),
    ("aegisflow.tools", "(key, order_id, amount, now()),",
     '(f"{key}:{runtime.current.new_id()}", order_id, amount, now()),'),
)
_NO_CAP_TRIGGER = (
    ("aegisflow.db", "WHEN COALESCE((SELECT SUM", "WHEN 0 AND COALESCE((SELECT SUM"),
)

MUTATIONS = {m.name: m for m in (
    # The three the brief names.
    Mutation("skip_idempotency_check", "the ledger ignores the idempotency key",
             # The cap refuses the duplicate money, so the damage shows up as a run misreporting its refund.
             ("effect_duplicated", "refund_cap", "undisclosed_effect_on_failed_step"), _IGNORE_REFUND_KEY),
    Mutation("skip_idempotency_check_and_cap", "the same, with the order cap also gone, so nothing stops a duplicate",
             ("effect_duplicated", "refund_cap"), _IGNORE_REFUND_KEY + _NO_CAP_TRIGGER),
    Mutation("record_progress_first", "issue_refund is recorded as done before the refund is attempted",
             ("succeeded_without_effect",),
             (("aegisflow.engine", "        try:\n            output = tools.call(conn, name, key, state, attempt)\n",
               _RECORD_FIRST),)),
    Mutation("drop_fsync", "commits are no longer synced, so power loss can erase acknowledged work",
             ("ack_lost", "orphan_effect"),
             (("aegisflow.db", 'SYNCHRONOUS = "FULL"', 'SYNCHRONOUS = "OFF"'),)),
    # The bugs the audit found, put back.
    Mutation("no_fencing", "a driver whose lease was taken over keeps writing (P2)",
             ("stale_write", "step_regressed", "history_mismatch", "integrity_error", "terminal_changed"),
             (("aegisflow.engine", "if current != epoch:", "if False:"),
              ("aegisflow.engine", 'if run["lease_epoch"] != epoch:', "if False:"))),
    Mutation("no_order_cap", "nothing limits total refunds to the order amount (P3)",
             ("refund_cap",),
             _NO_CAP_TRIGGER + (("aegisflow.tools", "if remaining <= 0:", "if False:"),
                                ("aegisflow.tools", '"refundable_paise": remaining',
                                 '"refundable_paise": order["total_paise"]'))),
    Mutation("ignore_request_key", "POST /runs ignores the client's idempotency key (P3)",
             ("request_ran_twice",),
             (("aegisflow.engine", "if idempotency_key is not None:", "if False:"),
              ("aegisflow.engine", "now(), now(), idempotency_key),", "now(), now(), None),"))),
    Mutation("lose_outcome_unknown", "a fatal error on a retried refund claims a known outcome (P1)",
             ("undisclosed_effect_on_failed_step",),
             (("aegisflow.engine", "if name in tools.SIDE_EFFECTING and pending is not None:", "if False:"),)),
    Mutation("hide_committed_effects", "a run failing after its refund doesn't say the refund happened (P7)",
             ("undisclosed_effect_before_failure",),
             (("aegisflow.engine", "if committed:", "if False:"),)),
    Mutation("volatile_backoff", "a takeover retries at once, ignoring the backoff still owed (P4)",
             ("backoff_skipped",),
             (("aegisflow.engine", 'if pending["next_attempt_at"] != waited_for:', "if False:"),)),
    Mutation("write_on_connect", "every connection, reads included, takes the write lock (P5)",
             ("read_path_writes",),
             (("aegisflow.db", 'if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:', "if True:"),)),
    Mutation("notify_ignores_key", "the message provider delivers again on a repeated key (P6)",
             ("notified_twice",),
             (("aegisflow.tools", "(key, to, message, now()),",
               '(f"{key}:{runtime.current.new_id()}", to, message, now()),'),)),
    # Other claims the checker makes.
    Mutation("no_backoff", "retries fire immediately", ("backoff_skipped",),
             (("aegisflow.engine",
               "runtime.current.sleep(delay)  # Outside tx(): never hold the write lock while waiting.", "pass"),)),
    Mutation("unbounded_retries", "the attempt limit is ignored", ("retry_limit",),
             (("aegisflow.engine", "if attempt < RETRY_POLICY.max_attempts:", "if True:"),)),
    Mutation("fresh_key_per_attempt", "each attempt uses a new idempotency key",
             # The cap again stops the money; the message goes out under a key the step never recorded.
             ("effect_duplicated", "refund_cap", "notified_twice", "succeeded_without_notify"),
             (("aegisflow.engine", "output = tools.call(conn, name, key, state, attempt)",
               'output = tools.call(conn, name, f"{key}:{attempt}", state, attempt)'),)),
    Mutation("no_lease_exclusion", "any caller can claim a run another driver holds",
             # Fencing still protects the data, so what's left to see is a displaced driver's backoff going unserved.
             ("stale_write", "history_mismatch", "integrity_error", "step_regressed", "backoff_skipped"),
             (("aegisflow.engine", "WHERE id=? AND (lease_token IS NULL OR lease_expires_at < ?)",
               "WHERE id=? AND (1 OR lease_expires_at < ?)"),)),
    Mutation("no_approval_gate", "refunds over the threshold skip approval", ("gate_bypassed",),
             (("aegisflow.engine", "if _needs_approval(conn, run_id, epoch, idx, name, state):", "if False:"),)),
    Mutation("split_progress_record", "a step's result and its event commit in separate transactions",
             ("history_mismatch",),
             (("aegisflow.engine", '                (run_id, idx, _dumps(state), now()),\n            )\n'
               '            _event(conn, run_id, "step_succeeded", {',
               '                (run_id, idx, _dumps(state), now()),\n            )\n'
               '        with _fenced_tx(conn, run_id, epoch):\n'
               '            _event(conn, run_id, "step_succeeded", {'),)),
    Mutation("decide_twice", "a decision applies even when no approval is pending",
             ("double_decision", "terminal_changed", "illegal_transition"),
             (("aegisflow.engine", "\"WHERE run_id=? AND status='pending' \"", '"WHERE run_id=? "'),
              ("aegisflow.engine", "WHERE id=? AND status='awaiting_approval')", "WHERE id=?)"))),
    Mutation("terminal_not_final", "advancing a failed run starts it going again",
             ("terminal_changed", "illegal_transition", "rerun_after_success"),
             (("aegisflow.engine", 'if run["status"] != "running":\n            return run["status"]',
               'if run["status"] in ("awaiting_approval", "rejected", "succeeded"):\n'
               '            return run["status"]'),)),
)}


# Not engine bugs: checker mistakes the simulator exposed. Each puts the mistake back,
# so a regression test can show that its seed really did report a false failure.
CHECKER_MISTAKES = {m.name: m for m in (
    Mutation("jump_window_from_commit", "the backoff's clock-jump exemption begins only when the failure commits",
             ("backoff_skipped",),
             (("aegisflow.sim.checker", "jumped = any(attempt_began <= t <= now for t in self.forward_jumps)",
               "jumped = any(failed_at <= t <= now for t in self.forward_jumps)"),)),
    Mutation("schema_judged_per_statement", "read-path writes are judged by the schema version at each statement",
             ("read_path_writes",),
             (("aegisflow.sim.checker", "if proc.readonly and verb in WRITE_VERBS and proc.schema_current_at_open:",
               'if proc.readonly and verb in WRITE_VERBS and self.world.keeper.execute("PRAGMA user_version")'
               '.fetchone()[0] == db.SCHEMA_VERSION:'),)),
)}


def _top_level_name(node: ast.stmt) -> str | None:
    if isinstance(node, ast.FunctionDef | ast.ClassDef):
        return node.name
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return None


def _segments(source: str) -> dict[str, tuple[ast.stmt, str]]:
    found = {}
    for node in ast.parse(source).body:
        name = _top_level_name(node)
        if name is not None:
            found[name] = (node, ast.get_source_segment(source, node) or "")
    return found


def _install(module_name: str, edits: list[tuple[str, str]]) -> list[tuple[object, str, object]]:
    """Re-execute only the definitions the edits changed. Returns how to undo it."""
    module = importlib.import_module(module_name)
    path = Path(module.__file__ or "")
    original = path.read_text(encoding="utf-8")
    source = original
    for old, new in edits:
        if source.count(old) != 1:
            raise ValueError(f"{module_name}: expected one match for {old!r}, found {source.count(old)}")
        source = source.replace(old, new)

    before = _segments(original)
    undo: list[tuple[object, str, object]] = []
    for name, (node, segment) in _segments(source).items():
        if before[name][1] == segment:
            continue
        code = compile(ast.Module([node], []), str(path), "exec")
        previous = getattr(module, name)
        exec(code, module.__dict__)
        replacement = getattr(module, name)
        if isinstance(node, ast.ClassDef):
            # Keep the class's identity: subclasses and isinstance checks hold the original.
            setattr(module, name, previous)
            for attr, value in vars(replacement).items():
                if callable(value):
                    undo.append((previous, attr, vars(previous)[attr]))
                    setattr(previous, attr, value)
        else:
            undo.append((module, name, previous))
    return undo


@contextmanager
def applied(name: str | None) -> Iterator[None]:
    if name is None:
        yield
        return
    by_module: dict[str, list[tuple[str, str]]] = {}
    for module_name, old, new in {**MUTATIONS, **CHECKER_MISTAKES}[name].edits:
        by_module.setdefault(module_name, []).append((old, new))
    undo: list[tuple[object, str, object]] = []
    try:
        for module_name, edits in by_module.items():
            undo.extend(_install(module_name, edits))
        yield
    finally:
        for owner, attr, value in reversed(undo):
            setattr(owner, attr, value)
