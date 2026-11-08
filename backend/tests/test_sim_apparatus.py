"""Mistakes in the simulator itself, each found as a false positive by a long run."""
import json
import sqlite3
from types import SimpleNamespace

from aegisflow.sim.checker import Checker
from aegisflow.sim.workload import make_plan
from aegisflow.sim.world import World


def _checker_over_one_failed_attempt():
    keeper = sqlite3.connect(":memory:")
    keeper.row_factory = sqlite3.Row
    keeper.executescript("""
        CREATE TABLE runs(id TEXT, status TEXT, lease_token TEXT);
        CREATE TABLE steps(run_id TEXT, idx INTEGER, status TEXT, attempt INTEGER);
        CREATE TABLE events(id INTEGER PRIMARY KEY, run_id TEXT, type TEXT, payload_json TEXT);
        INSERT INTO runs VALUES ('r', 'running', NULL);
        INSERT INTO steps VALUES ('r', 0, 'started', 2);
    """)
    keeper.execute("INSERT INTO events(run_id, type, payload_json) VALUES ('r', 'step_attempt_failed', ?)",
                   (json.dumps({"idx": 0, "attempt": 1, "next_delay_s": 0.1}),))
    world = SimpleNamespace(keeper=keeper, sched=SimpleNamespace(true_us=1_000, log=lambda message: None))
    checker = Checker(world)
    proc = SimpleNamespace(name="driver", claims={}, read_before_tx_us=None, started_us=None)
    checker.committed(proc)  # The attempt failed at 1 ms, so its backoff ends at 101 ms.
    return checker, world, proc


def test_a_failure_read_again_after_power_loss_keeps_its_backoff_from_when_it_failed():
    # Nightly seeds 2025602 and 2066987 at 1629a8f: the restart re-read the failure as if it had just happened.
    checker, world, proc = _checker_over_one_failed_attempt()
    world.sched.true_us = 150_000
    checker.power_restored()
    world.sched.true_us = 160_000  # 159 ms after the failure, 10 ms after the restart.
    checker.tool_started(proc, "fetch_order", "r:0")
    assert "backoff_skipped" not in checker.seen


def test_a_retry_inside_the_backoff_is_still_caught_across_power_loss():
    checker, world, proc = _checker_over_one_failed_attempt()
    world.sched.true_us = 20_000
    checker.power_restored()
    world.sched.true_us = 50_000
    checker.tool_started(proc, "fetch_order", "r:0")
    assert checker.seen["backoff_skipped"] > 0


def test_a_simulated_sleep_lasts_the_whole_time_asked_for(tmp_path):
    # Nightly seed 2048818 at 1629a8f: 8,176 us left of a backoff slept as 8,175.
    world = World(0, make_plan(0, set()), str(tmp_path / "sim.db"))
    slept = []

    def sleeper():
        before = world.sched.true_us
        world.runtime().sleep(0.008176)
        slept.append(world.sched.true_us - before)

    world.sched.spawn("sleeper", sleeper)
    world.sched.run_until_idle()
    assert slept == [8_176]
