"""python -m aegisflow.sim: run seeds, replay one, or survey what a range finds."""
import argparse
import sys
import time

from .run import Result, simulate
from .workload import FEATURES


def _features(text: str | None) -> set[str] | None:
    if text is None:
        return None
    chosen = {f for f in text.split(",") if f}
    unknown = chosen - set(FEATURES)
    if unknown:
        raise SystemExit(f"unknown features: {sorted(unknown)}; known: {', '.join(FEATURES)}")
    return chosen


def repro_command(result: Result, only: set[str] | None = None) -> str:
    command = f"python -m aegisflow.sim replay --seed {result.seed}"
    if only is not None:
        command += f" --only {','.join(sorted(only)) or ','}"
    return command


def _report_failure(result: Result) -> None:
    print(f"\nFAILED seed {result.seed}")
    for failure in result.failures():
        print(f"  {failure}")
    print(f"  reproduce: {repro_command(result)}")


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
    result = simulate(args.seed, _features(args.only), keep_trace=args.trace)
    if args.trace and result.trace:
        print("\n".join(result.trace))
    print(f"seed {result.seed}: digest {result.digest}, {result.steps} steps, faults {dict(result.stats)}")
    failures = result.failures()
    for failure in failures:
        print(f"  {failure}")
    print("FAILED" if failures else "ok")
    return 1 if failures else 0


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
    replay.set_defaults(fn=cmd_replay)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
