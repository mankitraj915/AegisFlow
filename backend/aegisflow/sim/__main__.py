"""python -m aegisflow.sim: run seeds, replay one, survey a range, or run the negative controls."""
import argparse
import sys
import time
from collections import Counter

from .checker import INVARIANTS
from .mutations import MUTATIONS, applied
from .run import Result, shrink, simulate
from .workload import FEATURES


def _features(text: str | None) -> set[str] | None:
    if text is None:
        return None
    chosen = {f for f in text.split(",") if f}
    unknown = chosen - set(FEATURES)
    if unknown:
        raise SystemExit(f"unknown features: {sorted(unknown)}; known: {', '.join(FEATURES)}")
    return chosen


def repro_command(seed: int, only: set[str] | None = None) -> str:
    command = f"python -m aegisflow.sim replay --seed {seed}"
    if only is not None:
        command += f" --only {','.join(sorted(only)) or ','}"
    return command


def _report_failure(result: Result) -> None:
    print(f"\nFAILED seed {result.seed}")
    for failure in result.failures():
        print(f"  {failure}")
    print(f"  reproduce: {repro_command(result.seed, shrink(result.seed, result.violations[0][0]))}")


def cmd_run(args) -> int:
    started = time.perf_counter()
    done = failed = 0
    seed = args.start
    while done < args.seeds:
        if args.budget_seconds is not None and time.perf_counter() - started >= args.budget_seconds:
            break
        result = simulate(seed)
        done += 1
        if result.failures():
            failed += 1
            _report_failure(result)
            if args.stop_first:
                break
        seed += 1
    elapsed = time.perf_counter() - started
    print(f"{done} seeds ({args.start}..{args.start + done - 1}) in {elapsed:.1f} s, {failed} failed")
    return 1 if failed else 0


def cmd_replay(args) -> int:
    with applied(args.mutation):
        result = simulate(args.seed, _features(args.only), keep_trace=args.trace)
    if args.trace and result.trace:
        print("\n".join(result.trace))
    print(f"seed {result.seed}: digest {result.digest}, {result.steps} steps, faults {dict(result.stats)}")
    failures = result.failures()
    for failure in failures:
        print(f"  {failure}")
    print("FAILED" if failures else "ok")
    return 1 if failures else 0


def cmd_survey(args) -> int:
    """How many seeds each invariant takes to break. Doesn't stop at the first failure."""
    started = time.perf_counter()
    first: dict[str, int] = {}
    count: Counter = Counter()
    for seed in range(args.start, args.start + args.seeds):
        for invariant in simulate(seed).invariants():
            first.setdefault(invariant, seed)
            count[invariant] += 1
    elapsed = time.perf_counter() - started
    print(f"{args.seeds} seeds ({args.start}..{seed}) in {elapsed:.1f} s")
    print(f"{'invariant':36} {'first seed':>10} {'seeds to find':>13} {'failing seeds':>13}")
    for invariant in INVARIANTS:
        if invariant in first:
            to_find = first[invariant] - args.start + 1
            print(f"{invariant:36} {first[invariant]:>10} {to_find:>13} {count[invariant]:>13}")
        else:
            print(f"{invariant:36} {'-':>10} {'-':>13} {0:>13}")
    return 1 if first else 0


def cmd_controls(args) -> int:
    """Put each known bug back and count the seeds until the checker catches it."""
    names = args.name or list(MUTATIONS)
    missed = 0
    print(f"{'control':32} {'caught by':34} {'seeds':>6} {'seconds':>8}")
    for name in names:
        mutation = MUTATIONS[name]
        started = time.perf_counter()
        caught = None
        other: set[str] = set()
        with applied(name):
            for seed in range(args.start, args.start + args.max_seeds):
                found = simulate(seed).invariants()
                hits = found & set(mutation.catches)
                other |= found - hits
                if hits:
                    caught = (sorted(hits)[0], seed - args.start + 1)
                    break
        elapsed = time.perf_counter() - started
        if caught:
            print(f"{name:32} {caught[0]:34} {caught[1]:>6} {elapsed:>8.1f}", flush=True)
        else:
            missed += 1
            detail = f"NOT CAUGHT in {args.max_seeds}"
            print(f"{name:32} {detail:34} {'-':>6} {elapsed:>8.1f}  other: {sorted(other)}", flush=True)
    return 1 if missed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aegisflow.sim")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run a range of seeds")
    run.add_argument("--seeds", type=int, default=1000)
    run.add_argument("--start", type=int, default=0)
    run.add_argument("--budget-seconds", type=float, help="stop starting new seeds after this long")
    run.add_argument("--stop-first", action="store_true")
    run.set_defaults(fn=cmd_run)

    replay = commands.add_parser("replay", help="run one seed again, exactly")
    replay.add_argument("--seed", type=int, required=True)
    replay.add_argument("--only", help="comma-separated features to enable, instead of the seed's own")
    replay.add_argument("--trace", action="store_true", help="print every scheduling step")
    replay.add_argument("--mutation", choices=sorted(MUTATIONS), help="with this negative control applied")
    replay.set_defaults(fn=cmd_replay)

    survey = commands.add_parser("survey", help="report how many seeds each invariant takes to break")
    survey.add_argument("--seeds", type=int, default=1000)
    survey.add_argument("--start", type=int, default=0)
    survey.set_defaults(fn=cmd_survey)

    controls = commands.add_parser("controls", help="reintroduce known bugs and count the seeds to catch each")
    controls.add_argument("--name", action="append", choices=sorted(MUTATIONS), help="only this control (repeatable)")
    controls.add_argument("--max-seeds", type=int, default=2000)
    controls.add_argument("--start", type=int, default=0)
    controls.set_defaults(fn=cmd_controls)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
