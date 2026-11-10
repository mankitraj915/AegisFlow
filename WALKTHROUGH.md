# Walkthrough: how AegisFlow is tested

AegisFlow's claim is that a refund workflow stays correct when machines and networks misbehave. This document explains how that claim is tested, why the design makes the testing possible, and where the evidence stops.

## Deterministic simulation in one page

A failure that needs a crash at one exact moment, while another request is halfway through, doesn't show up in ordinary tests and can't be reproduced if it shows up in production. Deterministic simulation fixes both problems: it takes control of everything nondeterministic and drives it from a single number, the seed.

One seed produces one run:

1. **A plan.** `workload.py` picks the client commands (one to three refund requests, some operator actions, sometimes repeated requests and readers) and which faults are on. Each seed turns on a random subset of features, which is called swarm testing. It matters because a fault that fires constantly can hide the rarer path another fault needs.
2. **A schedule.** The real, synchronous engine runs on ordinary threads, but only the thread holding a baton executes. A thread gives the baton back only inside a seam, and the seeded scheduler (`scheduler.py`) decides who goes next. Time is a counter that moves only there.
3. **Faults.** At each seam the seed can inject one: a process crash, power loss, a torn commit, an I/O error, a commit that is lost or that applies but reports failure, a forward or backward clock jump, or a lost, delayed, stalled, duplicated or late-arriving message to the external system. The provider can also misbehave on its own: report an error for a refund it made, answer a retry with the reply to an earlier failed request, have its status lookup fail, or forget keys older than a retention window of 310 to 700 s while an outage ages runs by up to 800 s. A poisoned step kills every process that reaches it.
4. **Clients.** Refund requests, operators, readers and, in some seeds, a recovery worker sweeping on a schedule, which can itself be killed mid-recovery. With `http_layer` on, some clients call the real FastAPI app in process instead of the engine, with and without `Idempotency-Key`, repeated and concurrent. With `large_workload` on, four to nine more runs share the database.
5. **Settling.** Faults stop, abandoned leases expire, and the worker or a simulated operator finishes anything unfinished. A held run is settled by a person who knows the truth. Whatever is still unfinished after that counts as stuck.
6. **Judgement.** `checker.py` watches every commit, tool call and reply during the run, then examines the final state. It reads the database and the external system directly and shares no logic with the engine. It knows what the provider really did, not what it said.

The same seed produces the same run, byte for byte: the same interleaving, the same database contents, the same digest. `tests/test_sim_determinism.py` checks this within a process and across processes with different hash seeds. It's the property everything else rests on, because without it a failing seed is worth nothing. Equality across operating systems or SQLite versions is not claimed.

When a seed fails, `run` prints it with a minimal command. Shrinking turns off each of the seed's features in turn and keeps those the failure needs. Each fault draws from its own random stream, so turning one off doesn't change the others' decisions. The features added during hardening draw their on/off decisions and their extra commands from one more stream, so adding them left every older seed's plan as it was.

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

`checker.py` defines 33 named invariants:

| Claim | Invariants |
|---|---|
| Every side effect happens at most once per step | `effect_duplicated`, `notified_twice` |
| Refunds never exceed the order amount, and money stays integer | `refund_cap`, `money_not_integer` |
| The ledger matches what the provider really did | `ledger_mismatch` |
| An unknown outcome is never reported as a plain failure | `unknown_reported_as_failure` |
| Finished stays finished, and nothing re-runs | `terminal_changed`, `illegal_transition`, `step_regressed`, `rerun_after_success` |
| A run is taken over only once its lease is up, and a replaced driver can't write | `two_drivers`, `stale_write` |
| Retries respect limits and backoff, and crash loops are bounded | `retry_limit`, `wrong_backoff_delay`, `backoff_skipped`, `crash_loop_unbounded` |
| Durable state matches event history | `history_mismatch`, `checkpoint_mismatch` |
| Acknowledged work survives power loss | `ack_lost`, `orphan_effect` |
| A success really happened | `succeeded_without_effect`, `succeeded_without_notify` |
| Approval means something | `gate_bypassed`, `effect_on_rejected`, `double_decision` |
| A failed run admits what committed | `undisclosed_effect_before_failure` |
| One request, one run, and the API keeps its contract | `request_ran_twice`, `api_contract` |
| Reads don't take the write lock | `read_path_writes` |
| Every run ends `succeeded`, `failed`, `rejected` or `quarantined`, and nothing blows up | `stuck_run`, `integrity_error`, `unexpected_exception`, `runaway` |

`unknown_reported_as_failure` replaced `undisclosed_effect_on_failed_step`. The old one accepted a run that failed at `issue_refund` with a refund in the ledger, as long as it said `outcome_unknown`. Once the engine can ask the provider, that answer is no longer good enough. The new one asks whether a run reported `failed` for a step the provider really carried out, unless it was quarantined and said the outcome was unknown.

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

- It's **at-most-once effect plus at-least-once delivery**, which together behave as exactly once *only because the receiver deduplicates*. A provider that ignored keys or expired them too soon would break this. The production mock does neither. The simulator's provider can forget keys, which is what the next section is about.
- The key must be durable before the effect happens. Step 1 commits with `synchronous=FULL`, and the key is also derived from the run id and step index, so it can't change as long as the run exists.

`record_progress_first` and `skip_idempotency_check_and_cap` are the controls for the two ways of getting this wrong.

## Reconciliation: why ask before retrying

The argument above assumes an error means "try again". Against a real provider it doesn't. When a refund call returns an error, any of these can be true:

- the refund didn't happen;
- it happened and the reply was lost;
- it happened and the provider reported an error anyway;
- the reply belongs to an earlier, failed request on the same key;
- an earlier request is still in flight and will land later.

Retrying is safe in all of these only while the provider still remembers the key. Reporting `failed` is wrong in all but the first. So before calling a side-effecting step again, and before reporting any error from one, the engine asks the provider about the key through its own seam (`runtime.current.lookup`):

- **The effect is there.** The step is recorded as succeeded with the provider's output, marked `step_reconciled`, and not called again.
- **No record, and the key is young** (under `KEY_TRUST_S`, 300 s). The provider can't have forgotten it, so a retry under the same key is safe. "No record" isn't proof of failure, though. A first attempt refused with no record is a known failure. A later one, with an earlier request that may still arrive, isn't, and the run is held.
- **No record and an old key, or the lookup fails.** Nobody can tell. The run goes to `needs_review`, which no driver leaves on its own. A later lookup that finds the effect (`reconcile`, run by every recovery sweep) or a person (`resolve`) settles it, and a person can't deny an effect the provider shows.

The order matters. Retrying first, against a provider that has forgotten the key, makes a second refund the provider has no way to recognise. Asking first turns that into a lookup. The controls show it: `retry_before_reconcile` and `trust_forgotten_keys` are both caught as a second notification, and `no_reconciliation` as a ledger that disagrees with the provider.

How much this was exercised, from 1,500 seeds counted at the Stage 1 commit: the lookup found the effect 198 times, found no record 269 times and left a doubt 104 times. Of the holds, 14 were forgotten keys, 13 were requests possibly still in flight and 24 were failed lookups. A run focused on forgetting, with clock jumps and key retention on, held 37 runs for forgotten keys, and the provider never saw a forgotten key repeated.

The trust window has a hole: key age is measured on the wall clock. A backward clock jump could make an old key look young and get its "no record" believed. That reasoning is recorded as a weak point. No seed has reached it.

## Recovery, and steps that keep crashing

A recovery worker (`recovery.sweep`) runs in the API process. Each sweep resumes running runs whose lease has expired, or that nobody has claimed and that have been quiet for a lease's length. It also asks the provider again about every held run. It takes the lease and the fencing epoch through the same `_claim` as every other driver, so a worker that stalls or dies mid-recovery is fenced off like anything else. Interrupted runs are left for a person, because the demo's interruption is meant to be resumed by hand.

A worker that resumes everything has a new failure mode: a step that kills its process every time gets resumed forever. Crashes still don't use up retry attempts, but each step now has an `executing` flag, set while a driver works on it. Finding the flag still set means the last driver stopped without an outcome, and that counts as a crash of that step. At `MAX_CRASHES` (3) the run is `quarantined`: it stops for good with its history intact, and a side-effecting step says its outcome is unknown.

From 1,500 seeds at the Stage 2 commit: 806 recover calls, 174 sweeps killed mid-recovery, 1,089 crashes noted, 162 runs quarantined, and no invariant failed.

## The HTTP layer

Seeds with `http_layer` send some client commands through the real FastAPI app, in process over ASGI, built under the simulator's runtime, so the routing, validation, header handling and error mapping are the code that ships. Requests come with and without `Idempotency-Key`, repeated and concurrent. `api_contract` checks that each reply is one the API documents: 200 or 201 for a create, and 200, 404 or 409 for an operation. A request key must never produce two 201s.

This is how the simulator found its only engine bug since the audit. `POST /runs` creates the run and then drives it. If another driver claimed the run in between, the takeover's conflict came back as a 409 for a request that had succeeded. A client with no idempotency key takes that to mean nothing happened and tries again, which makes a second run. Calling the engine directly hid it, because the conflict looked like any other. Fixed in `15706ec`. See [docs/BUGS_FOUND.md](docs/BUGS_FOUND.md).

## Swarm or everything on

Swarm testing turns each feature on with a 40% chance per seed. The alternative is every fault on, every time. `controls --mode all` measures that against the default, over the same seed ranges.

Measured at `d4c7194` with `controls --max-seeds 300`, every control in each mode, over seeds 100,000 to 100,299 and 200,000 to 200,299:

| Seeds from | Mode | Controls caught (of 30) | Seconds for all 30 |
|---|---|---:|---:|
| 100,000 | swarm | 24 | 444.3 |
| 100,000 | all on | 25 | 903.8 |
| 200,000 | swarm | 27 | 286.8 |
| 200,000 | all on | 23 | 941.0 |

Where both modes caught a control, swarm needed fewer seeds for 7 controls and all-on for 11, with 5 ties, in the first range. In the second it was 9 to 7, with 7 ties. Some controls were caught by one mode only. In the first range, all-on alone caught `lose_outcome_unknown` and `volatile_backoff`, and swarm alone caught `decide_twice`. In the second, swarm alone caught `no_reconciliation`, `retry_before_reconcile`, `unknown_as_failure` and `trust_forgotten_keys`. An all-on seed is slower to run: 300 seeds that caught nothing took 45 to 59 s in swarm mode and 92 to 114 s with everything on.

Two ranges of 300 seeds is a small sample, and which mode wins a given control changes between them. What it supports: per seed, neither mode is clearly better. Per second, swarm is ahead here, and each mode catches controls the other misses, so turning everything on is a useful second pass, not a replacement. In the second range, the all-on runs also reported `two_drivers` on seeds that never produced the expected catch. That is probably the checker mistake fixed in `45e07e7` (see [docs/BUGS_FOUND.md](docs/BUGS_FOUND.md)), but those seeds weren't rerun.

## Fencing tokens: why expiry isn't enough

A lease says "only one driver at a time", and for a while the code took that at face value. It isn't true. A lease is a promise about time, and the holder can't observe time passing while it stalls. A driver can pause inside a slow tool call or a GC pause, or a forward clock jump can expire its lease while it's perfectly healthy. A second driver then takes over, correctly. The first wakes up and carries on writing, because nothing has told it otherwise. The audit showed the result: a finished run flipping from `succeeded` to `failed`.

No lease duration fixes this. The stall can always be longer than the lease. The check has to happen where the write happens, and that's what a fencing token does:

- Each claim increments `runs.lease_epoch`, a number that only goes up, and the driver keeps its value.
- Every write a driver makes goes through `engine._fenced_tx`, which checks the epoch **inside the same transaction** as the write. If the run has moved to a newer epoch, it raises `LeaseLost` and writes nothing.

The check and the write being atomic is the whole point. Checking first and writing in a later transaction would leave exactly the gap we started with. The clock now decides only *when a takeover may happen*. It no longer decides *whose writes count*, so clock jumps can cost time but can't corrupt state.

The fence stops at the database. A stale driver can't be stopped from calling the external system, because it's already mid-call by the time it's replaced, and the provider knows nothing about epochs. That case is covered by the idempotency key rather than the epoch: a step's key is fixed for the life of the step, so the stale driver's call and the new driver's call are the same effect under the same key. It lands once however they interleave. If a call may still be in flight when the run would fail, the run is held for review with the key, because at that point nothing else would be honest.

The controls back this up. `no_fencing` is caught. `no_lease_exclusion`, which removes the lease check but keeps the fence, took 4,413 seeds at `eb6cac7` and was caught only as a skipped backoff, not as corrupted data. Since `two_drivers` was added it's caught within a few seeds as what it is, a takeover of a live lease (see the table in [docs/BUGS_FOUND.md](docs/BUGS_FOUND.md)). With fencing in place, the lease looks to be mostly about not wasting work, though a test can't prove that it's never needed.

## What this can and can't catch

It can find bugs in how the engine interleaves, retries, recovers and reports, under the fault model above, in the workload the generator produces.

It can't find:

- **Anything outside the fault model.** Disks that lie about fsync, kernel or SQLite bugs, crashes during a WAL checkpoint, and torn pages in the main database file.
- **Real parallelism.** Threads take turns, so this explores interleavings at seam boundaries. Separate real processes on a real file are tested only by `test_resume_after_real_process_death`.
- **Real external systems.** The ledger and the message provider are mocks written by the same author. The simulator's provider now lies, goes stale, loses its lookup and forgets keys, but only in the ways that author thought of. A real provider could fail in ways nobody modelled: partial refunds, a lookup that is itself stale, or a retention window shorter than `KEY_TRUST_S`.
- **Violations nobody wrote an invariant for.** The checker knows only what it's told. Its author also wrote the engine and shares its blind spots. During this work the checker had false positives and the workload had gaps, and they came to light only by exercising them.
- **Rare paths in a small number of seeds.** `volatile_backoff`, a real bug class, took 2,990 seeds when it was first measured. A clean run is evidence, not proof. It shows nothing was found in those seeds.
- **The HTTP server around the app.** The simulator calls the FastAPI app in process. Uvicorn, sockets, timeouts and real concurrent requests aren't simulated.
- **The background thread itself.** The simulator runs `recovery.sweep` as one of its clients. The thread and timer in the API lifespan are covered by ordinary tests.

Known behaviours that are deliberate:

- A forward clock jump can shorten a backoff, because the deadline is on the wall clock. The checker exempts jumps in that span. A backward jump can't stretch one by more than `RETRY_POLICY.max_delay_s`, because the wait is capped.
- Key age for `KEY_TRUST_S` is on the wall clock too. A backward jump could make an old key look trustworthy. This is a gap, not a choice, and it's listed as one.
- Crashes don't use up retry attempts. They count towards `MAX_CRASHES` instead, which quarantines the run.
- The worker doesn't resume interrupted runs. Those wait for a person.
- A person settling a held run can be wrong whenever the provider can't contradict them.
- The `checkpoints` table holds audit snapshots. Recovery rebuilds state from steps and never reads it.

## Questions you may be asked

**Is this formally verified?** No. It's systematic testing over seeds. It can show bugs exist, and it reports what didn't fail in the seeds it ran.

**How is this different from fuzzing?** A fuzzer varies inputs and looks for crashes. This controls the schedule, the clock and the faults too, and it judges behaviour against a history checker, so wrong answers count as well as crashes. And any failure replays exactly.

**Why threads, if only one runs at a time?** The engine is ordinary synchronous code. Baton-passing lets it run unmodified, with no async rewrite and no separate model, which means the bugs found are in the code that ships.

**Doesn't the simulator's SQLite run with sync off?** Yes, for speed, because power loss is modelled by cutting the WAL rather than by a real power cut. The engine's declared level still decides how far the cut can reach. That's why `drop_fsync` is caught even though no real fsync happens in either case.

**What did the simulator find that you didn't already know?** One engine bug: `POST /runs` answered 409 for a run it had created, found once clients went through the HTTP layer. The audit had already found the seven bugs it rediscovered. The provider weaknesses it confirmed were predicted before the adversarial provider was written. Everything else it reported was a mistake in the checker, the workload or the simulator. That includes the first scheduled long run's three failures, which were two ways of mistiming a backoff: one after a power loss, one by a microsecond of rounding. The next two nightly runs failed 5 more seeds the same two ways. The final survey found a third mistiming: a lease timed from the wrong clock read, which hid a real forward jump. That's in `BUGS_FOUND.md`, split by who found what first.

**Why a database trigger for the refund cap instead of a check in code?** A check in code reads the total and then inserts, and two runs can both pass the check. The policy step does exactly that, as `test_runs_racing_for_one_order_refund_it_once` shows. The trigger runs inside the insert, under the write lock.

**Why does the cap live in the "external" ledger's table?** Because the party that moves money is the one that can enforce it. In a real system this would be the provider's rule or a reconciliation job, not something this repository could guarantee.

**What's the weakest part?** The fsync model, the provider model and the people. The first two are assumptions about things outside the code that the tests can't see past. The third is a person settling a held run when the provider can't contradict them.

**What happens if the provider forgets a key?** A retry under that key would pay twice, which is why the engine asks before retrying and trusts "no record" only for a young key. An old key with no record holds the run instead. The design still depends on the provider keeping keys for longer than `KEY_TRUST_S`.

**Why not use Temporal or a similar engine?** For a real product you probably should. This repository exists to show the failure modes and how to test for them, on a laptop with no budget.
