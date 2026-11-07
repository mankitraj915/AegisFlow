# Walkthrough: how AegisFlow is tested

AegisFlow's claim is that a refund workflow stays correct when machines and networks misbehave. This document explains how that claim is tested, why the design makes the testing possible, and where the evidence stops.

## Deterministic simulation in one page

A failure that needs a crash at one exact moment, while another request is halfway through, doesn't show up in ordinary tests and can't be reproduced if it shows up in production. Deterministic simulation fixes both problems: it takes control of everything nondeterministic and drives it from a single number, the seed.

One seed produces one run:

1. **A plan.** `workload.py` picks the client commands (one to three refund requests, some operator actions, sometimes repeated requests and readers) and which faults are on. Each seed turns on a random subset of features, which is called swarm testing. It matters because a fault that fires constantly can hide the rarer path another fault needs.
2. **A schedule.** The real, synchronous engine runs on ordinary threads, but only the thread holding a baton executes. A thread gives the baton back only inside a seam, and the seeded scheduler (`scheduler.py`) decides who goes next. Time is a counter that moves only there.
3. **Faults.** At each seam the seed can inject one: a process crash, power loss, a torn commit, an I/O error, a commit that is lost or that applies but reports failure, a forward or backward clock jump, or a lost, delayed, stalled, duplicated or late-arriving message to the external system.
4. **Settling.** Faults stop, abandoned leases expire, and a simulated operator finishes anything unfinished. Whatever is still unfinished after that counts as stuck.
5. **Judgement.** `checker.py` watches every commit, tool call and reply during the run, then examines the final state. It reads the database and the external system directly and shares no logic with the engine.

The same seed produces the same run, byte for byte: the same interleaving, the same database contents, the same digest. `tests/test_sim_determinism.py` checks this within a process and across processes with different hash seeds. It's the property everything else rests on, because without it a failing seed is worth nothing. Equality across operating systems or SQLite versions is not claimed.

When a seed fails, `run` prints it with a minimal command. Shrinking turns off each of the seed's features in turn and keeps those the failure needs. Each fault draws from its own random stream, so turning one off doesn't change the others' decisions.

```powershell
cd backend
.venv\Scripts\python.exe -m aegisflow.sim run --seeds 1000                  # stop with --budget-seconds N
.venv\Scripts\python.exe -m aegisflow.sim replay --seed 42 --trace          # every scheduling step of one seed
.venv\Scripts\python.exe -m aegisflow.sim survey --seeds 5000               # how many seeds each invariant takes to break
.venv\Scripts\python.exe -m aegisflow.sim controls                          # put each known bug back and count seeds to catch it
```

## Why seams make this possible

Simulation only works if nothing escapes it. If the engine read the real clock once, or called `uuid4` once, a seed would stop determining the run. So everything the engine can't decide for itself goes through `aegisflow/runtime.py`: the clock, new identifiers, sleeping, opening a database, and calling a tool or the external ledger. Production installs the real implementations, and the simulator installs controlled ones. The engine code under test is the same code that runs in production, not a model of it.

Nothing relies on discipline to keep this true. `tests/test_seams.py` is a static check over the engine's source that fails on banned modules and calls, such as `time`, `random`, `threading` or `datetime.now`.

Process death needs no seam of its own. A simulated process dies at whichever seam it reaches next, which covers every point where the engine touches anything outside its own memory. The kill raises `Killed`, a `BaseException`, and every later seam call raises it again, so `finally` blocks can't reach the outside world. That matches SIGKILL: a dying process can't release its own lease.

## Storage: real SQLite, modelled durability

Each simulated process gets a real stdlib connection to a real WAL database, so locking and isolation are SQLite's own. The `SimConn` wrapper adds a seam before each statement and turns lock contention into simulated waiting.

Power loss is the part that can't be observed directly, because stdlib `sqlite3` has no hook on fsync. So durability is **modelled from SQLite's documentation, not observed**. In WAL mode, `synchronous=FULL` syncs the WAL at each commit and `OFF` never does. `DurableWal` tracks the WAL offset that would have reached disk under the level the engine asked for, and a power loss keeps the main database file plus the WAL cut at a byte offset at or beyond that point. SQLite's own recovery then opens that image. A torn commit cuts the WAL inside the commit it just made.

This is a good test of what the engine does with its durability settings. It says nothing about whether a disk honours fsync. Auto-checkpointing is off in the simulator, so **crashes during a WAL checkpoint and torn pages in the main database file are not covered**.

## The invariants

`checker.py` defines 29 named invariants, among them:

| Claim | Invariants |
|---|---|
| Every side effect happens at most once per step | `effect_duplicated`, `notified_twice` |
| Refunds never exceed the order amount | `refund_cap` |
| Finished stays finished, and nothing re-runs | `terminal_changed`, `illegal_transition`, `step_regressed`, `rerun_after_success` |
| A replaced driver can't write | `stale_write` |
| Retries respect limits and backoff | `retry_limit`, `wrong_backoff_delay`, `backoff_skipped` |
| Durable state matches event history | `history_mismatch`, `checkpoint_mismatch` |
| Acknowledged work survives power loss | `ack_lost`, `orphan_effect` |
| A success really happened | `succeeded_without_effect`, `succeeded_without_notify` |
| Approval means something | `gate_bypassed`, `effect_on_rejected`, `double_decision` |
| A failed run admits what committed | `undisclosed_effect_on_failed_step`, `undisclosed_effect_before_failure` |
| One request, one run | `request_ran_twice` |
| Reads don't take the write lock | `read_path_writes` |
| Nothing is stuck or blows up | `stuck_run`, `integrity_error`, `unexpected_exception`, `runaway` |

## Testing the tests

A checker that never fails proves nothing, so `mutations.py` puts known bugs back. Each control is an exact edit to the real source, applied in memory for the length of a `with` block, and it raises if its target text has changed, so a control can't silently test nothing. [`docs/BUGS_FOUND.md`](docs/BUGS_FOUND.md) reports how many seeds each one took.

The controls earned their keep beyond the numbers. They showed that operators in the workload almost never acted on runs that had moved on, which is why `stale_operators` exists, and they showed the checker crashing on a corrupt checkpoint.

## Why a crash between the side effect and its record can't duplicate

This is the central design point. The order of operations for a side-effecting step is:

1. Commit the step row as `started`, holding its key `{run_id}:{idx}`.
2. Call the external system with that key. It commits its own row under the key.
3. Commit the step as `succeeded`.

A crash between 2 and 3 leaves the effect committed and the step `started`. Recovery finds the `started` step and calls again with **the stored key, not a new one**. The external system looks the key up and inserts in one transaction, so the second call finds the row and replays it. The engine doesn't need to know which side of the crash it landed on, because both outcomes end the same way: one row.

The argument has assumptions, and they are the honest limits of "exactly once":

- It's **at-most-once effect plus at-least-once delivery**, which together behave as exactly once *only because the receiver deduplicates*. A provider that ignored keys or expired them too soon would break this. Here the provider is a mock that does neither.
- The key must be durable before the effect happens. Step 1 commits with `synchronous=FULL`, and the key is also derived from the run id and step index, so it can't change as long as the run exists.

`record_progress_first` and `skip_idempotency_check_and_cap` are the controls for the two ways of getting this wrong.

## Fencing tokens: why expiry isn't enough

A lease says "only one driver at a time", and for a while the code took that at face value. It isn't true. A lease is a promise about time, and the holder can't observe time passing while it stalls. A driver can pause inside a slow tool call or a GC pause, or a forward clock jump can expire its lease while it's perfectly healthy. A second driver then takes over, correctly. The first wakes up and carries on writing, because nothing has told it otherwise. The audit showed the result: a finished run flipping from `succeeded` to `failed`.

No lease duration fixes this. The stall can always be longer than the lease. The check has to happen where the write happens, and that's what a fencing token does:

- Each claim increments `runs.lease_epoch`, a number that only goes up, and the driver keeps its value.
- Every write a driver makes goes through `engine._fenced_tx`, which checks the epoch **inside the same transaction** as the write. If the run has moved to a newer epoch, it raises `LeaseLost` and writes nothing.

The check and the write being atomic is the whole point. Checking first and writing in a later transaction would leave exactly the gap we started with. The clock now decides only *when a takeover may happen*. It no longer decides *whose writes count*, so clock jumps can cost time but can't corrupt state.

The fence stops at the database. A stale driver can't be stopped from calling the external system, because it's already mid-call by the time it's replaced, and the provider knows nothing about epochs. That case is covered by the idempotency key rather than the epoch: a step's key is fixed for the life of the step, so the stale driver's call and the new driver's call are the same effect under the same key. It lands once however they interleave. If a run then fails while a call may still be in flight, it reports `outcome_unknown` with the key, because at that point nothing else would be honest.

The controls back this up. `no_fencing` is caught. `no_lease_exclusion`, which removes the lease check but keeps the fence, took 4,413 seeds and was caught as a skipped backoff, not as corrupted data. With fencing in place, the lease looks to be mostly about not wasting work, though a test can't prove that it's never needed.

## What this can and can't catch

It can find bugs in how the engine interleaves, retries, recovers and reports, under the fault model above, in the workload the generator produces.

It can't find:

- **Anything outside the fault model.** Disks that lie about fsync, kernel or SQLite bugs, crashes during a WAL checkpoint, and torn pages in the main database file.
- **Real parallelism.** Threads take turns, so this explores interleavings at seam boundaries. Separate real processes on a real file are tested only by `test_resume_after_real_process_death`.
- **Real external systems.** The ledger and the message provider are mocks written by the same author, and they honour keys perfectly. Real providers have retention windows, partial failures and outages.
- **Violations nobody wrote an invariant for.** The checker knows only what it's told. Its author also wrote the engine and shares its blind spots. During this work the checker had false positives and the workload had a gap, and both came to light only by exercising them.
- **Rare paths in a small number of seeds.** `volatile_backoff`, a real bug class, took 2,990 seeds. A clean run is evidence, not proof. It shows nothing was found in those seeds.
- **HTTP behaviour.** The simulator calls the engine directly. The API layer is covered by ordinary tests.

Known behaviours that are deliberate:

- A forward clock jump can shorten a backoff, because the deadline is on the wall clock. The checker exempts jumps in that span. A backward jump can't stretch one by more than `RETRY_POLICY.max_delay_s`, because the wait is capped.
- A crash loop isn't bounded. Crashes don't use up retry attempts, by design, so a step that kills its process every time will be resumed for as long as someone keeps resuming it.
- Nothing resumes stuck runs automatically. The simulated operator stands in for a person.
- The `checkpoints` table holds audit snapshots. Recovery rebuilds state from steps and never reads it.

## Questions you may be asked

**Is this formally verified?** No. It's systematic testing over seeds. It can show bugs exist, and it reports what didn't fail in the seeds it ran.

**How is this different from fuzzing?** A fuzzer varies inputs and looks for crashes. This controls the schedule, the clock and the faults too, and it judges behaviour against a history checker, so wrong answers count as well as crashes. And any failure replays exactly.

**Why threads, if only one runs at a time?** The engine is ordinary synchronous code. Baton-passing lets it run unmodified, with no async rewrite and no separate model, which means the bugs found are in the code that ships.

**Doesn't the simulator's SQLite run with sync off?** Yes, for speed, because power loss is modelled by cutting the WAL rather than by a real power cut. The engine's declared level still decides how far the cut can reach. That's why `drop_fsync` is caught even though no real fsync happens in either case.

**What did the simulator find that you didn't already know?** No engine bugs. The audit had already found the seven it rediscovered, and after those fixes every failure it reported turned out to be a mistake in the checker. That's the honest answer, and it's in `BUGS_FOUND.md`. A reasonable reading is that the engine is small and had just been audited closely, not that the simulator is unable to find anything: the controls show that it can.

**Why a database trigger for the refund cap instead of a check in code?** A check in code reads the total and then inserts, and two runs can both pass the check. The policy step does exactly that, as `test_runs_racing_for_one_order_refund_it_once` shows. The trigger runs inside the insert, under the write lock.

**Why does the cap live in the "external" ledger's table?** Because the party that moves money is the one that can enforce it. In a real system this would be the provider's rule or a reconciliation job, not something this repository could guarantee.

**What's the weakest part?** The fsync model and the mock provider. Both are assumptions about things outside the code, and the tests can't see past them.

**What happens if the idempotency key table were lost?** The provider would pay twice. The design depends on the provider's record of keys outliving any retry.

**Why not use Temporal or a similar engine?** For a real product you probably should. This repository exists to show the failure modes and how to test for them, on a laptop with no budget.
