"""One simulated world: the scheduler, a real database under faults, a separate
external system, and the clients that drive the engine."""
import os
import random
import sqlite3
from collections import Counter
from functools import partial

from .. import db, engine, runtime, tools
from ..models import Scenario
from .scheduler import Killed, Proc, Scheduler
from .storage import DurableWal, SimConn, raw_connect
from .workload import Command, Plan

OPERATOR_ROUNDS = 12
EXPECTED_ERRORS = (engine.Conflict, engine.RunNotFound, engine.OrderNotFound)


class World:
    def __init__(self, seed: int, plan: Plan, path: str, keep_trace: bool = False):
        self.seed, self.plan, self.path = seed, plan, path
        self.sched = Scheduler(random.Random(f"{seed}:sched"), plan.stickiness, keep_trace)
        self.sched.on_step = self._on_step
        self._streams = {f: random.Random(f"{seed}:{f}") for f in plan.rates}
        self._values = random.Random(f"{seed}:values")
        self._ids = random.Random(f"{seed}:ids")
        self.faults_on = True
        self.wal = DurableWal(path)
        self.stats: Counter = Counter()
        self.observers: list = []
        self.deliveries: Counter = Counter()
        self._invoking_key = ""
        self.keeper = raw_connect(path)  # Holds the WAL open: no last-close checkpoint.
        self.keeper.execute("PRAGMA journal_mode=WAL")
        self.external = self._open_external()
        self.ledger = tools.SqlLedger(self.external)

    @staticmethod
    def _open_external() -> sqlite3.Connection:
        # Survives anything that happens to engine storage, like a real payment system.
        conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        db.ensure_schema(conn)
        return conn

    def runtime(self) -> runtime.Runtime:
        return runtime.Runtime(
            clock=self.sched.now,
            new_id=lambda: f"{self._ids.getrandbits(128):032x}",
            sleep=lambda seconds: self.sched.sleep_us(int(seconds * 1_000_000)),
            open_db=self._open_db,
            invoke=self._invoke,
            ledger=lambda conn: self.ledger,
            send_message=self._send_message,
        )

    def on(self, fault: str) -> bool:
        """Always draws, so a fault's decisions don't depend on which others are on."""
        fires = self._streams[fault].random() < self.plan.rates[fault]
        if fires and self.faults_on and fault in self.plan.enabled:
            self.stats[fault] += 1
            self.sched.log(f"fault {fault}")
            return True
        return False

    def _notify(self, method: str, *args) -> None:
        for observer in self.observers:
            getattr(observer, method)(*args)

    # ---- seams ----

    def _open_db(self, path: str) -> SimConn:
        self.sched.step("open")
        assert self.sched.current is not None
        return SimConn(self, self.sched.current)

    def _on_step(self, proc: Proc, label: str) -> None:
        if proc.daemon:
            return
        if self.on("clock_jump"):
            self.sched.skew_us += self._values.choice([-1, 1]) * self._values.randrange(1_000_000, 120_000_000)
            self._notify("clock_jumped", self.sched.skew_us)
        if self.on("power_loss"):
            self.power_loss()
        if self.on("crash"):
            proc.killed = True
            raise Killed

    def power_loss(self, torn_from: int | None = None) -> None:
        image = self.wal.image(self._values, torn_from)
        self.sched.kill_all()

        def restart() -> None:
            self.keeper.close()
            self.wal.restore(image)
            self.keeper = raw_connect(self.path)
            self.sched.log("restart after power loss")
            self._notify("power_restored")

        self.sched.after_kill = restart
        raise Killed

    def note_statement(self, proc: Proc, verb: str) -> None:
        self._notify("statement", proc, verb)

    def after_commit(self, proc: Proc) -> None:
        self.wal.committed()
        self.stats["commits"] += 1
        self._notify("committed", proc)

    def _invoke(self, name: str, key: str, call) -> dict:
        sched = self.sched
        proc = sched.current
        self._notify("tool_started", proc, name, key)
        sched.step(f"tool {name}")
        if self.on("tool_stall"):
            sched.sleep_us(self._values.randrange(61_000_000, 130_000_000))  # Beyond the lease.
        elif self.on("tool_delay"):
            sched.sleep_us(self._values.randrange(1_000, 3_000_000))
        if self.on("tool_drop_request"):
            raise tools.RetryableToolError("simulated: request lost")
        if self.on("tool_fatal"):
            raise tools.ToolError("simulated: gateway refused the call")
        if self.on("tool_late_delivery"):
            delay = self._values.randrange(1_000, 90_000_000)
            sched.spawn(f"net-{key}", lambda: self._deliver_late(key, call), delay, daemon=True)
            raise tools.RetryableToolError("simulated: timed out, request still in flight")
        output = self._apply(key, call)
        if self.on("tool_duplicate"):
            self._apply(key, call)
        sched.step(f"tool-done {name}")
        if self.on("tool_drop_response"):
            raise tools.RetryableToolError("simulated: response lost")
        return output

    def _apply(self, key: str, call) -> dict:
        self._invoking_key = key
        output = call()
        self._notify("external_changed")
        return output

    def _deliver_late(self, key: str, call) -> None:
        try:
            self._apply(key, call)
        except tools.ToolError:
            pass  # The caller gave up long ago; nobody hears the refusal.

    def _send_message(self, to: str, message: str) -> None:
        self.deliveries[self._invoking_key] += 1

    # ---- clients ----

    def start(self) -> None:
        for number, command in enumerate(self.plan.commands):
            self.sched.spawn(f"{command.kind}-{number}", partial(self._handle, command), command.at_us)

    def _handle(self, command: Command) -> None:
        proc = self.sched.current
        assert proc is not None
        proc.readonly = command.kind == "read"
        conn = None
        try:
            conn = db.connect(self.path)
            getattr(self, f"_do_{command.kind}")(conn, command)
        except EXPECTED_ERRORS as exc:
            self._notify("replied", proc, command.kind, type(exc).__name__, None)
        except sqlite3.Error as exc:
            self.stats["server_errors"] += 1
            self._notify("server_error", proc, command.kind, exc)

    def _do_create(self, conn, command: Command) -> None:
        run_id = engine.start_run(conn, command.order_id, Scenario(**command.scenario))
        run = engine.get_run(conn, run_id)
        self._notify("request_replied", command.request_id, run_id)
        self._notify("replied", self.sched.current, "create", run.status, run_id)

    def _pick_run(self, conn, statuses: set[str], pick: float) -> str | None:
        eligible = [r.id for r in engine.list_runs(conn) if r.status in statuses]
        return eligible[int(pick * len(eligible))] if eligible else None

    def _do_operate(self, conn, command: Command) -> None:
        action = command.action
        if action in ("approve", "reject"):
            run_id = self._pick_run(conn, {"awaiting_approval"}, command.pick)
        else:
            run_id = self._pick_run(conn, {"running", "awaiting_approval"}, command.pick)
        if run_id is None:
            return
        if action == "approve":
            engine.approve(conn, run_id, "operator")
        elif action == "reject":
            engine.reject(conn, run_id, "operator", "simulated")
        else:
            getattr(engine, action)(conn, run_id)
        run = engine.get_run(conn, run_id)
        self._notify("replied", self.sched.current, action, run.status, run_id)

    def _do_read(self, conn, command: Command) -> None:
        run_id = self._pick_run(conn, {"running", "awaiting_approval", "succeeded", "failed", "rejected"}, command.pick)
        if run_id is not None:
            engine.get_run_detail(conn, run_id)
            engine.list_events(conn, run_id)

    def _operator_pass(self) -> None:
        """A person going through what's unfinished, once nothing else is failing."""
        conn = db.connect(self.path)
        for run in engine.list_runs(conn):
            try:
                if run.status == "running":
                    engine.resume(conn, run.id)
                elif run.status == "awaiting_approval":
                    if self._values.random() < 0.7:
                        engine.approve(conn, run.id, "operator")
                    else:
                        engine.reject(conn, run.id, "operator", "simulated")
            except engine.Conflict:
                pass

    def unfinished(self) -> list[str]:
        rows = self.keeper.execute("SELECT id FROM runs WHERE status IN ('running', 'awaiting_approval') ORDER BY id")
        return [r["id"] for r in rows]

    def settle(self) -> None:
        """Run the plan under faults, then stop the faults and let an operator
        finish up. Whatever's still unfinished after that is stuck for good."""
        self.sched.run_until_idle()
        self.faults_on = False
        db.ensure_schema(self.keeper)  # A crash may have beaten every connect() to it.
        for number in range(OPERATOR_ROUNDS):
            if not self.unfinished():
                return
            self.sched.true_us += (engine.LEASE_S + 1) * 1_000_000  # Abandoned leases expire.
            self.sched.spawn(f"operator-{number}", self._operator_pass)
            self.sched.run_until_idle()

    def dump(self) -> bytes:
        engine_dump = "\n".join(self.keeper.iterdump())
        external_dump = "\n".join(self.external.iterdump())
        return f"{engine_dump}\n--- external ---\n{external_dump}".encode()

    def close(self) -> None:
        self.keeper.close()
        self.external.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)
