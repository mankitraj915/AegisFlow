"""The recovery worker: one pass over everything left without a driver.

A sweep decides nothing by itself. It takes each run's lease through the engine
like any other caller, so it can't displace a live driver before the lease runs
out, and a driver it does replace is fenced off. Sweeps can overlap or die halfway:
whatever one leaves undone, the next finds still undone.
"""
from dataclasses import dataclass, field

from . import engine


@dataclass
class Sweep:
    recovered: dict[str, str] = field(default_factory=dict)  # run id -> status it reached
    rechecked: dict[str, str] = field(default_factory=dict)
    errors: list[tuple[str, Exception]] = field(default_factory=list)


def _attempt(results: dict[str, str], errors: list, settle, conn, run_id: str) -> None:
    try:
        results[run_id] = settle(conn, run_id)
    except (engine.Conflict, engine.RunNotFound):
        pass  # Someone else has it, or got there first. Nothing to do.
    except Exception as exc:  # One bad run mustn't starve the rest. Its crash count still rises.
        errors.append((run_id, exc))


def sweep(conn) -> Sweep:
    """Resume abandoned runs, and ask the provider again about held ones."""
    result = Sweep()
    for run_id in engine.abandoned_runs(conn):
        _attempt(result.recovered, result.errors, engine.recover, conn, run_id)
    for run_id in engine.held_runs(conn):
        _attempt(result.rechecked, result.errors, engine.reconcile, conn, run_id)
    return result
