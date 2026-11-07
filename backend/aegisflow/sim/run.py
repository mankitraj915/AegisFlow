"""Run one seed: same seed, same bytes."""
import os
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass, field

from .. import runtime
from .workload import make_plan
from .world import World

_STACK_BYTES = 512 * 1024
_workdir: str | None = None


def _path(seed: int) -> str:
    global _workdir
    if _workdir is None:
        _workdir = tempfile.mkdtemp(prefix="aegisflow-sim-")
    return os.path.join(_workdir, f"seed-{seed}.db")


@dataclass
class Result:
    seed: int
    digest: str
    dump: bytes = b""
    trace: list[str] | None = None
    stats: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    steps: int = 0
    runaway: bool = False

    def failures(self) -> list[str]:
        found = [f"unexpected_exception: {e.strip().splitlines()[-1]}" for e in self.errors]
        if self.runaway:
            found.append("runaway: the run never stopped")
        return found


def simulate(seed: int, only: set[str] | None = None, without: frozenset[str] = frozenset(),
             keep_trace: bool = False) -> Result:
    previous_stack = threading.stack_size(_STACK_BYTES)
    world = World(seed, make_plan(seed, only, without), _path(seed), keep_trace)
    try:
        with runtime.use(world.runtime()):
            world.start()
            world.settle()
        dump = world.dump()
        world.sched.update_digest(dump)
        sched = world.sched
        return Result(
            seed, sched.digest(), dump, sched.trace, world.stats,
            [p.error for p in sched.procs if p.error], sched.steps, sched.runaway,
        )
    finally:
        world.close()
        threading.stack_size(previous_stack)
