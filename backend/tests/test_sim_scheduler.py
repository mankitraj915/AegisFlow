import random

import pytest

from aegisflow.sim.scheduler import Killed, Scheduler


def interleaving(seed):
    sched = Scheduler(random.Random(seed))
    order = []

    def worker(name):
        def body():
            for i in range(5):
                order.append((name, i))
                sched.step("work")
        return body

    for name in "abc":
        sched.spawn(name, worker(name))
    sched.run_until_idle()
    return order


def test_same_seed_gives_the_same_interleaving():
    assert interleaving(7) == interleaving(7)


def test_different_seeds_explore_different_interleavings():
    assert len({tuple(interleaving(seed)) for seed in range(20)}) > 1


def test_each_process_still_runs_in_its_own_order():
    order = interleaving(3)
    for name in "abc":
        assert [i for n, i in order if n == name] == list(range(5))


def test_sleep_uses_simulated_time_and_wakes_in_order():
    sched = Scheduler(random.Random(1))
    woke = []

    def sleeper(name, us):
        def body():
            sched.sleep_us(us)
            woke.append((name, sched.true_us >= us))
        return body

    sched.spawn("long", sleeper("long", 3_600_000_000))
    sched.spawn("short", sleeper("short", 1_000_000))
    sched.run_until_idle()
    assert woke == [("short", True), ("long", True)]
    assert sched.true_us >= 3_600_000_000  # An hour, without really waiting.


def test_spawned_process_starts_after_its_delay():
    sched = Scheduler(random.Random(1))
    started = []
    sched.spawn("late", lambda: started.append(sched.true_us), delay_us=5_000)
    sched.run_until_idle()
    assert started[0] >= 5_000


def test_a_killed_process_cannot_reach_a_seam_from_finally():
    sched = Scheduler(random.Random(1))
    reached = []

    def victim():
        try:
            sched.sleep_us(1_000_000_000)
            reached.append("woke")
        finally:
            sched.step("cleanup")  # Raises: like a real kill, cleanup never reaches the outside.
            reached.append("cleaned up")

    def killer():
        sched.sleep_us(1_000)
        victim_proc.killed = True

    victim_proc = sched.spawn("victim", victim)
    sched.spawn("killer", killer)
    sched.run_until_idle()
    assert reached == [] and victim_proc.error is None and victim_proc.state == "done"


def test_a_crash_in_a_process_is_reported_not_swallowed():
    sched = Scheduler(random.Random(1))

    def broken():
        raise ValueError("boom")

    proc = sched.spawn("broken", broken)
    sched.run_until_idle()
    assert "ValueError: boom" in proc.error


def test_killed_is_not_an_exception_handlers_can_swallow():
    assert not issubclass(Killed, Exception)
    with pytest.raises(Killed):
        try:
            raise Killed
        except Exception:  # noqa: B014 - the point of the test
            pass
