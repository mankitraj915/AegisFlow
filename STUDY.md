# Study guide

The questions to be able to answer about this work, stage by stage, with answers. [WALKTHROUGH.md](WALKTHROUGH.md) has the longer explanations.

## Stage 0: the audit

**What did the audit find, and why not just start building the simulator?**
Seven real bugs, P1 to P7 in [docs/AUDIT.md](docs/AUDIT.md), including a repeated `POST /runs` refunding ₹2,598 on a ₹1,299 order and a finished run flipping from `succeeded` to `failed`. The audit decided what the simulator had to check. It also set the order of work: the simulator had to re-find each bug on unfixed code before any fix, otherwise it could be blind to them and nobody would know.

**The project already had exactly-once claims and passing tests. How did those bugs survive?**
The tests checked what the author thought of: the crash window inside one run. They never tried the same request twice, a lease expiring under a live driver, or a fatal error on a retry. Passing tests said the listed cases worked, not that the claim held.

**Why are audit bugs and simulator bugs kept apart?**
Because invariants written after reading a bug are likely to catch that bug. Re-finding P1 to P7 shows the simulator can reach and recognise them, not that it would have found them blind. Merging the lists would overstate what it can do.

## Stage 1: seams

**What is a seam, and which ones are there?**
A single point where the engine reaches something it doesn't control, which can be swapped: clock, new ids, sleep, opening a database, tool invocation, and the external ledger. They are all in `aegisflow/runtime.py`. Production uses the real ones, and the simulator installs controlled ones.

**How do you know nothing bypasses them?**
`tests/test_seams.py` parses the engine's source and fails on banned imports and calls (`time`, `random`, `threading`, `datetime.now`, `sqlite3.connect` and others). It's a static check. It wouldn't catch nondeterminism in a dependency, but the byte-for-byte tests would.

**Why is there no seam for process death?**
A simulated process dies at the next seam it reaches, and every point where the engine touches the outside is a seam. Between seams it's only changing its own memory, so a crash there looks the same as a crash at the next seam.

## Stage 2: the simulator

**What makes a run deterministic when it uses real threads and real SQLite?**
Only the thread with the baton runs, and it gives the baton back only inside a seam, so the seeded scheduler decides the interleaving. Time is a counter. SQLite is deterministic given the same statements in the same order. Lock contention is resolved by simulated waiting with zero real timeout, so real timing never enters.

**Why does each fault have its own random stream?**
So turning one off leaves the others' decisions unchanged. Otherwise removing a feature would produce a completely different run, and shrinking couldn't tell whether the failure needed that feature.

**How do you simulate power loss with no fsync hook?**
By modelling it. From SQLite's documentation, `synchronous=FULL` in WAL mode syncs at each commit. `DurableWal` tracks the offset that would have reached disk, and a power loss keeps the main file plus the WAL cut at a random offset at or beyond it. SQLite's real recovery then opens that image. It's a model of the disk, not an observation of one. WAL checkpoint crashes aren't covered.

## Stage 3: invariants

**Why is the checker separate from the engine, and what does it read?**
So an engine bug can't hide by agreeing with itself. The checker reads the database and the external system directly and rebuilds each run from its events alone, then compares that with the tables.

**How do you check "every side effect exactly once" given a crash can hide it?**
The external system is outside the crash, and it counts deliveries per key. At the end, each run must have at most one refund, totals must stay within the order amount, a succeeded run must have its refund and message, and a failed run with a refund must disclose it.

**How do you know the checker can actually fail?**
The negative controls: 20 known bugs put back as exact source edits, each with a count of the seeds to catch it. See [docs/BUGS_FOUND.md](docs/BUGS_FOUND.md). They also exposed a workload gap and a checker crash.

## Stage 4: bugs and controls

**What did the simulator find on its own?**
No engine bugs. After the audit fixes, every failure it reported was a mistake in the checker: `read_path_writes` misjudging connections that opened before the schema existed, and `backoff_skipped` timing a backoff from the commit instead of the clock read. Saying that plainly is the point of the provenance split. Do not claim otherwise.

**The hardest question: why can't a crash between a side effect and its progress record cause a duplicate in this design?**
Because the retry doesn't try a new effect. It repeats the same one, and the receiver can tell.

1. Before calling anything, the engine commits the step as `started` with its key, `{run_id}:{idx}`. The key is durable before the effect can happen, and it's derived from the run and step, so it can't change.
2. The external system does the lookup and the insert in one transaction under that key.
3. Say the process dies right after the insert commits and before the step is recorded. On disk: the effect exists, and the step says `started`.
4. Recovery sees a `started` step. It doesn't create a new step or a new key. It calls again with the stored key. The system finds the row and replays it. One row.

If the crash came *before* the insert, the same call performs it for the first time. So recovery doesn't need to know which case it's in, and that's why the window is safe. The engine can't know, and doesn't have to.

Three things this rests on, which a good interviewer will push on:

- **The receiver has to deduplicate.** This is at-least-once delivery plus an idempotent receiver, not magic. A provider without keys, or one that forgets them too soon, breaks it. Here the provider is a mock.
- **Recording progress first doesn't work.** That's the other order, and it loses the effect rather than duplicating it. The `record_progress_first` control shows this: 9 seeds to catch.
- **Concurrency doesn't change the argument.** A stale driver mid-call and its replacement use the same key, because the key belongs to the step, not to the driver. The epoch fences database writes, and the key covers the external call, which can't be fenced.

**What does the `skip_idempotency_check` control show?**
That defences overlap. Alone it took 109 seeds and was caught not as a double refund, because the cap trigger refused the duplicate money, but as a run misreporting its outcome. With the cap also removed, it's caught as `refund_cap` in 9 seeds. So the cap would have saved the money while the bookkeeping went wrong.

**Why were fixes checked by reintroducing the bug rather than by replaying the discovery seed?**
Discovery seeds stop reaching the same code once the workload or the engine changes. Discovery seeds stay replayable at `005e746`, but the lasting regression test pins a current seed that passes on the fixed code and fails with the bug put back.

## Stage 5: claims and limits

**What exactly is claimed, and what isn't?**
Claimed: within the fault model and the workload, across the seeds reported, no invariant was violated, and each control was caught within a stated number of seeds. Not claimed: formal proof, uptime, performance, real-provider behaviour, or behaviour on real disks.

**What are the weakest points?**
Fsync is modelled, not observed. WAL checkpoint crashes aren't covered. The external system is a mock that honours keys perfectly. The invariants share an author with the engine. The workload is small, at most a few runs per seed. There are no real parallel processes except one process-death test.

**Explain fencing tokens in three sentences.**
A lease can expire under a healthy driver, so the driver can't rely on still holding it. Each claim increments a number kept on the run, and every write checks that number in the same transaction as the write, so a replaced driver's writes change nothing. The clock decides when a takeover may happen, and only the number decides whose writes count.
