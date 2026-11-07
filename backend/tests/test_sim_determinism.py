"""Same seed, same run, byte for byte. Without this a failing seed is worth nothing."""
import json
import os
import subprocess
import sys

import pytest

from aegisflow.sim.run import simulate

SEEDS = range(12)


@pytest.mark.parametrize("seed", SEEDS)
def test_same_seed_is_byte_identical(seed):
    a = simulate(seed, keep_trace=True)
    b = simulate(seed, keep_trace=True)
    assert a.trace == b.trace
    assert a.dump == b.dump
    assert a.digest == b.digest


def test_different_seeds_are_different_runs():
    assert len({simulate(seed).digest for seed in SEEDS}) == len(SEEDS)


def test_seeds_do_real_work():
    results = [simulate(seed) for seed in range(30)]
    faults = sum((r.stats for r in results), start=results[0].stats.__class__())
    for fault in ("crash", "power_loss", "torn_commit", "io_error", "commit_unknown", "clock_jump",
                  "tool_drop_response", "tool_stall", "tool_late_delivery"):
        assert faults[fault] > 0, fault
    assert faults["commits"] > 100


def digest_in_subprocess(hash_seed: str) -> str:
    code = (
        "import json; from aegisflow.sim.run import simulate;"
        "print(json.dumps([simulate(s).digest for s in (1, 2, 3, 4)]))"
    )
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout


def test_same_digest_across_processes_and_hash_seeds():
    in_this_process = [simulate(s).digest for s in (1, 2, 3, 4)]
    assert json.loads(digest_in_subprocess("1")) == in_this_process
    assert json.loads(digest_in_subprocess("4242")) == in_this_process
