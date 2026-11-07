"""Run one seed: same seed, same bytes."""
import atexit
import os
import shutil
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass, field

from .. import runtime
from .checker import Checker
from .workload import make_plan
from .world import World

_STACK_BYTES = 512 * 1024
_workdir: str | None = None


def _path(seed: int) -> str:
    global _workdir
    if _workdir is None:
        _workdir = tempfile.mkdtemp(prefix="aegisflow-sim-")
        atexit.register(shutil.rmtree, _workdir, ignore_errors=True)
    return os.path.join(_workdir, f"seed-{seed}.db")


@dataclass
class Result:
    seed: int
    digest: str
    dump: bytes = b""
    trace: list[str] | None = None
    stats: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    violations: list[tuple[str, str]] = field(default_factory=list)
    steps: int = 0
    enabled: set[str] = field(default_factory=set)

    def failures(self) -> list[str]:
        return [f"{name}: {detail}" for name, detail in self.violations]

    def invariants(self) -> set[str]:
        return {name for name, _ in self.violations}


def simulate(seed: int, only: set[str] | None = None, without: frozenset[str] = frozenset(),
             keep_trace: bool = False) -> Result:
    previous_stack = threading.stack_size(_STACK_BYTES)
    plan = make_plan(seed, only, without)
    world = World(seed, plan, _path(seed), keep_trace)
    checker = Checker(world)
    world.observers.append(checker)
    try:
        with runtime.use(world.runtime()):
            world.start()
            world.settle()
            checker.finish()
        dump = world.dump()
        world.sched.update_digest(dump)
        sched = world.sched
        return Result(
            seed, sched.digest(), dump, sched.trace, world.stats,
            [p.error for p in sched.procs if p.error], checker.violations, sched.steps, plan.enabled,
        )
    finally:
        world.close()
        threading.stack_size(previous_stack)


def shrink(seed: int, invariant: str) -> set[str]:
    """The smallest set of features found to still break `invariant` on this seed."""
    enabled = set(simulate(seed).enabled)
    for feature in sorted(enabled):
        candidate = enabled - {feature}
        if invariant in simulate(seed, only=candidate).invariants():
            enabled = candidate
    return enabled
