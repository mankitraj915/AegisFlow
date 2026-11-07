"""Seeded cooperative scheduler.

The real, synchronous engine runs on ordinary threads, but only the thread
holding the baton ever executes. A thread gives the baton back only inside a
seam (step or sleep), and the seed decides who gets it next, so the interleaving
is a pure function of the seed. Time is a counter that moves only here.
"""
import hashlib
import random
import threading
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
STEP_LIMIT = 50_000


class Killed(BaseException):
    """The process is dead. Every later seam call raises it again, so `finally`
    blocks can't reach anything outside the process, as with a real SIGKILL."""


class Proc:
    def __init__(self, pid: int, name: str, fn: Callable[[], None], wake_us: int, daemon: bool):
        self.pid, self.name, self.fn, self.wake_us, self.daemon = pid, name, fn, wake_us, daemon
        self.state = "sleeping"  # sleeping | ready | done
        self.killed = False
        self.started_us: int | None = None
        self.error: str | None = None
        self.thread: threading.Thread | None = None
        self.gate = threading.Lock()
        self.gate.acquire()
        self.conns: list = []
        self.readonly = False
        self.schema_current_at_open = False
        self.clock_read_us: int | None = None  # True time of this process's latest clock read.
        self.read_before_tx_us: int | None = None  # The same, as it stood when its transaction began.
        self.claims: dict[str, int] = {}


class Scheduler:
    def __init__(self, rng: random.Random, stickiness: float = 0.0, keep_trace: bool = False):
        self.rng, self.stickiness = rng, stickiness
        self.true_us = 0
        self.skew_us = 0
        self.procs: list[Proc] = []
        self.current: Proc | None = None
        self.steps = 0
        self.runaway = False
        self.on_step: Callable[[Proc, str], None] | None = None
        self.after_kill: Callable[[], None] | None = None
        self._digest = hashlib.sha256()
        self.trace: list[str] | None = [] if keep_trace else None
        self._baton = threading.Lock()
        self._baton.acquire()

    def now(self) -> datetime:
        return EPOCH + timedelta(microseconds=self.true_us + self.skew_us)

    def log(self, message: str) -> None:
        who = self.current.name if self.current else "world"
        line = f"{self.true_us} {self.skew_us} {who} {message}\n"
        self._digest.update(line.encode())
        if self.trace is not None:
            self.trace.append(line.rstrip("\n"))

    def digest(self) -> str:
        return self._digest.hexdigest()

    def update_digest(self, data: bytes) -> None:
        self._digest.update(data)

    def spawn(self, name: str, fn: Callable[[], None], delay_us: int = 0, daemon: bool = False) -> Proc:
        proc = Proc(len(self.procs), name, fn, self.true_us + delay_us, daemon)
        self.procs.append(proc)
        return proc

    # ---- called on a process's own thread ----

    def step(self, label: str) -> None:
        """A seam: time passes, another process may run, and this one may die."""
        proc = self.current
        assert proc is not None
        if proc.killed:
            raise Killed
        self.steps += 1
        if self.steps > STEP_LIMIT:
            self.runaway = True
            self.kill_all()
            raise Killed
        self.true_us += self.rng.randrange(1, 2000)
        self.log(label)
        if self.rng.random() >= self.stickiness:
            proc.state = "ready"
            self._yield(proc)
        if self.on_step is not None:
            self.on_step(proc, label)

    def sleep_us(self, us: int) -> None:
        proc = self.current
        assert proc is not None
        if proc.killed:
            raise Killed
        self.log(f"sleep {us}")
        proc.state, proc.wake_us = "sleeping", self.true_us + us
        self._yield(proc)

    def _yield(self, proc: Proc) -> None:
        self._baton.release()
        proc.gate.acquire()
        if proc.killed:
            raise Killed

    def _body(self, proc: Proc) -> None:
        try:
            proc.fn()
        except Killed:
            pass
        except BaseException:
            proc.error = traceback.format_exc()
        finally:
            for conn in proc.conns:
                conn.close()
            proc.state = "done"
            self._baton.release()

    # ---- called on the main thread ----

    def kill_all(self) -> None:
        for proc in self.procs:
            if proc.state != "done" and proc.thread is not None and not proc.daemon:
                proc.killed = True

    def _pick(self) -> Proc | None:
        live = [p for p in self.procs if p.state != "done"]
        if self.runaway:  # Give up on this run: nothing new starts or wakes.
            for proc in live:
                if proc.thread is None:
                    proc.state = "done"
                else:
                    proc.killed = True
            live = [p for p in live if p.thread is not None]
        doomed = [p for p in live if p.killed]
        if doomed:
            return doomed[0]
        if self.after_kill is not None:  # Every dying process has unwound.
            action, self.after_kill = self.after_kill, None
            action()
        for proc in live:
            if proc.state == "sleeping" and proc.wake_us <= self.true_us:
                proc.state = "ready"
        ready = [p for p in live if p.state == "ready"]
        if not ready:
            if not live:
                return None
            self.true_us = min(p.wake_us for p in live)
            ready = [p for p in live if p.wake_us <= self.true_us]
            for proc in ready:
                proc.state = "ready"
        return self.rng.choice(ready)

    def run_until_idle(self) -> None:
        while (proc := self._pick()) is not None:
            self.current = proc
            if proc.thread is None:
                proc.started_us = self.true_us
                proc.thread = threading.Thread(target=self._body, args=(proc,), daemon=True)
                proc.thread.start()
            else:
                proc.gate.release()
            self._baton.acquire()
            if proc.state == "done":
                proc.thread.join()
            self.current = None
