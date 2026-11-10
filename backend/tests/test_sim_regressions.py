"""Every bug the simulator has found stays found.

Each entry names a seed that catches the bug with it put back. Seeds are pinned
to the current workload generator; if that changes, `python -m aegisflow.sim
controls` prints the new ones. docs/BUGS_FOUND.md has the write-ups.
"""
import pytest

from aegisflow import db
from aegisflow.sim.mutations import CHECKER_MISTAKES, MUTATIONS, applied
from aegisflow.sim.run import simulate

# (control, seed, invariant it breaks)
ENGINE_BUGS = [
    ("lose_outcome_unknown", 439, "unknown_reported_as_failure"),  # P1
    ("no_fencing", 102, "history_mismatch"),  # P2
    ("no_order_cap", 8, "refund_cap"),  # P3
    ("ignore_request_key", 26, "request_ran_twice"),  # P3
    ("volatile_backoff", 142, "backoff_skipped"),  # P4
    ("write_on_connect", 2, "read_path_writes"),  # P5
    ("notify_ignores_key", 14, "notified_twice"),  # P6
    ("create_reports_conflict", 34, "api_contract"),
    ("hide_committed_effects", 4, "undisclosed_effect_before_failure"),  # P7
]

# Each guarantee added with reconciliation, recovery and the HTTP layer, taken away again.
GUARANTEES = [
    ("no_reconciliation", 5, "ledger_mismatch"),
    ("retry_before_reconcile", 12, "notified_twice"),
    ("unknown_as_failure", 12, "unknown_reported_as_failure"),
    ("trust_forgotten_keys", 12, "notified_twice"),
    ("no_crash_loop_bound", 7, "crash_loop_unbounded"),
    ("recovery_without_fencing", 32, "integrity_error"),
    ("worker_ignores_lease", 1, "two_drivers"),
    ("worker_does_nothing", 1, "stuck_run"),
    ("api_ignores_idempotency", 27, "api_contract"),
]

# The bug classes the design has to rule out.
CLASSES = [
    ("skip_idempotency_check", 5, "ledger_mismatch"),
    ("skip_idempotency_check_and_cap", 8, "refund_cap"),
    ("record_progress_first", 1, "succeeded_without_effect"),
    ("drop_fsync", 1, "ack_lost"),
]


@pytest.mark.parametrize("control,seed,invariant", ENGINE_BUGS + GUARANTEES + CLASSES)
def test_seed_passes_fixed_and_fails_with_the_bug_back(control, seed, invariant):
    assert simulate(seed).failures() == []
    with applied(control):
        assert invariant in simulate(seed).invariants()


# Where each audit bug was first rediscovered, at commit 005e746. The workload has
# changed since, so these are now just more seeds that must stay clean.
@pytest.mark.parametrize("seed", [2, 3, 6, 13, 14, 256, 551, 1545, 2951])
def test_discovery_seeds_stay_clean(seed):
    assert simulate(seed).failures() == []


# Checker mistakes the simulator exposed: (mistake, seed that reported a false failure).
@pytest.mark.parametrize("mistake,seed", [("backoff_timed_from_commit", 1750), ("schema_judged_per_statement", 4),
                                                  ("lease_timed_from_restart", 1),
                                                  ("lease_timed_from_commit", 2935)])
def test_seed_that_exposed_a_checker_mistake_stays_clean(mistake, seed):
    assert simulate(seed).failures() == []
    with applied(mistake):
        assert set(CHECKER_MISTAKES[mistake].catches) & simulate(seed).invariants()


def test_a_range_of_seeds_is_clean():
    for seed in range(100):
        assert simulate(seed).failures() == [], f"python -m aegisflow.sim replay --seed {seed}"


@pytest.mark.parametrize("mutation", list(MUTATIONS.values()) + list(CHECKER_MISTAKES.values()), ids=lambda m: m.name)
def test_every_control_still_applies_and_restores(mutation):
    before = (db.SYNCHRONOUS, db._migrate, db.ensure_schema)
    with applied(mutation.name):
        pass
    assert (db.SYNCHRONOUS, db._migrate, db.ensure_schema) == before


def test_a_control_whose_target_changed_is_an_error(monkeypatch):
    broken = MUTATIONS["drop_fsync"].__class__("broken", "", (), (("aegisflow.db", "no such text", "x"),))
    monkeypatch.setitem(MUTATIONS, "broken", broken)
    with pytest.raises(ValueError, match="expected one match"):
        with applied("broken"):
            pass


def test_fsync_control_really_changes_the_setting():
    with applied("drop_fsync"):
        assert db.SYNCHRONOUS == "OFF"
    assert db.SYNCHRONOUS == "FULL"
