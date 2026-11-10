# Bugs found

Provenance matters here, so bugs are split by who found them first:

- **Part A: audit-probe bugs.** Found by reading the code and probing it by hand (see [AUDIT.md](AUDIT.md)), before the simulator existed. The simulator then re-found each one on the unfixed engine.
- **Part B: simulator bugs.** First found by the simulator, with nobody having predicted them.

The simulator's invariants were written after the audit, by the same author. Part A shows the simulator can reach these bugs and recognise them, not that it found them blind.

## Part A: audit-probe bugs

### Rediscovery on the unfixed engine

Run at commit `005e746`, before any fix, with `python -m aegisflow.sim survey --seeds 6000`. It took 404.8 s. "Seeds to find" counts from seed 0 through the first failing seed.

| Audit | Bug | Invariant that caught it | First seed | Seeds to find | Failing seeds of 6,000 |
|---|---|---|---|---|---|
| P5 | Opening a connection takes the write lock, even to read | `read_path_writes` | 2 | 3 | 2,338 |
| P3 | A repeated request refunds an order twice | `refund_cap` | 3 | 4 | 908 |
| P3 | The same request runs as two runs | `request_ran_twice` | 6 | 7 | 318 |
| P6 | `notify_customer` delivers twice after a crash | `notified_twice` | 3 | 4 | 350 |
| P2 | A driver writes after losing its lease | `stale_write` | 13 | 14 | 28 |
| P2 | Same cause: a succeeded step overwritten as failed | `step_regressed` | 13 | 14 | 2 |
| P2 | Same cause: a run with two terminal events | `history_mismatch` | 551 | 552 | 3 |
| P2 | Same cause: two drivers race for a step index, HTTP 500 | `integrity_error` | 2951 | 2,952 | 4 |
| P7 | A run fails after its refund committed, without saying so | `undisclosed_effect_before_failure` | 14 | 15 | 170 |
| P1 | A committed refund, then a fatal error, reports plain `failed` | `undisclosed_effect_on_failed_step` | 256 | 257 | 19 |
| P4 | A retry skips its backoff after a crash or takeover | `backoff_skipped` | 1545 | 1,546 | 3 |

To replay any of these, check out `005e746` and run `python -m aegisflow.sim replay --seed <seed> --trace`.

**A gap in the rediscovery of P2.** The audit probe showed a *finished run* flipping from `succeeded` to `failed`. The `terminal_changed` invariant watches for that and did not fire in these 6,000 seeds. The seeds above show the same root cause through a step regressing and through two terminal events on one run, but not that exact symptom. The deeper survey below closes this gap.

### Deeper survey at the discovery commit

`survey --seeds 14000 --start 6000` at `005e746`, 1,012.5 s. `terminal_changed`, the exact P2 flip, did fire: first at seed 13007, once in those 14,000 seeds. `illegal_transition` also fired once, at seed 13817.

### The bugs, one by one

Schedules below come from `replay --seed <seed> --trace` at `005e746`, with the features the seed needs after shrinking. The regression seed is different from the discovery seed, because the workload has changed since, and it was recomputed again after the hardening work changed the workload. It's pinned in `backend/tests/test_sim_regressions.py`, where it must pass on the fixed code and break the named invariant with the bug put back.

#### P1: a committed refund, then a fatal error, reports plain `failed`

- **Discovery seed:** 256. Features: `duplicate_commands`, `tool_fatal`, `tool_late_delivery`, `tool_stall`.
- **Schedule:** on ORD-1001, the first `issue_refund` attempt fails with a retryable error and its effect isn't known to have been applied. The retry at 103.28 s gets a fatal error, so the run ends `failed`. The ledger holds a refund under that step's key, and the run's failure record doesn't mention it.
- **Root cause:** only retry exhaustion recorded `outcome_unknown`. The fatal path assumed a fatal error meant nothing had happened, which isn't true of a retry.
- **Fix:** `7db7bb2`. A fatal error on a side-effecting step that has already been attempted records the key and `outcome_unknown`.
- **Regression:** control `lose_outcome_unknown`, seed 439, now caught as `unknown_reported_as_failure`.

#### P2: a driver writes after losing its lease

- **Discovery seed:** 13 (`stale_write`). Features: `io_error`, `tool_stall`. The same cause showed up at seeds 551 and 2951 through forward clock jumps that expired a live lease.
- **Schedule:** `create-0` stalls in `fetch_order` at 0.04 s, past the 60 s lease. `operate-1` takes over at 70.9 s and fails on an I/O error. `create-0` wakes and writes `step_started` at 91.08 s.
- **Root cause:** a lease can expire under a driver that doesn't know, and nothing checked ownership at write time.
- **Fix:** `70e9855`. Each claim increments `runs.lease_epoch`, and every driver write checks it in the same transaction. [WALKTHROUGH.md](../WALKTHROUGH.md) has the reasoning.
- **Regression:** control `no_fencing`, seed 102, and `test_a_stale_driver_cannot_write_after_a_newer_one_took_over` for the exact symptom the audit probe showed.

#### P3: a repeated request refunds an order twice

- **Discovery seeds:** 3 (`refund_cap`, feature: `power_loss`) and 6 (`request_ran_twice`).
- **Schedule:** seed 3 has two separate requests for ORD-1001, so it shows the missing cap rather than a repeated request. A power loss at 0.46 s stops one run. At about 110.7 s an operator resumes it and approves the other, and both refund: 259,800 paise of 129,900. Seed 6 sends `req-0` twice, and it runs as two runs.
- **Root cause:** nothing tied a client request to a run, and nothing limited refunds across runs. Each run's key only stopped duplicates within that run.
- **Fix:** `ada9639` adds an optional `Idempotency-Key` on `POST /runs`, backed by a unique index. `0a09e56` adds a database trigger that refuses any refund past the order total. The tests that refunded one order twice were updated, and the demo data was reseeded with a pool of orders.
- **Regression:** controls `ignore_request_key`, seed 26, and `no_order_cap`, seed 8.

#### P4: a retry skips its backoff after a takeover

- **Discovery seed:** 1545. Features: `clock_jump`, `tool_delay`, `tool_drop_request`.
- **Schedule:** a forward clock jump at 0.2435 s expires `create-0`'s lease. Its `check_refund_policy` attempt then fails at 0.2462 s, and it commits the failure and sleeps 100 ms. `operate-3` takes over and retries 65 ms later. The jump came before the attempt, so it didn't shorten the backoff. The backoff was simply lost, because it lived only in the sleeping driver's memory.
- **Root cause:** the end of the backoff wasn't stored.
- **Fix:** `1290ae8`. `steps.next_attempt_at` stores it, and whoever picks the step up waits out the rest, capped at the policy's maximum in case the clock has jumped back.
- **Regression:** control `volatile_backoff`, seed 142.

#### P5: opening a connection takes the write lock

- **Discovery seed:** 2. Feature: `readers`.
- **Schedule:** `read-3` opens a connection at 99.5 s, long after the schema exists, and runs the schema script on a read-only path.
- **Root cause:** every `connect()` ran the schema and migration, including `GET /health`.
- **Fix:** `4213f93`. `PRAGMA user_version` records the schema version, and a current database skips the work.
- **Regression:** control `write_on_connect`, seed 2.

#### P6: `notify_customer` delivers twice

- **Discovery seed:** 3 (`notified_twice`). Feature: `scenario_faults`.
- **Schedule:** the second attempt at `notify_customer`, at 0.89 s, delivers and is interrupted before the step is recorded (`interrupt_after_commit`). The operator resumes it at 110.75 s and it delivers again. Two deliveries under one key.
- **Root cause:** the tool ignored its idempotency key and wasn't treated as side-effecting.
- **Fix:** `9c99d58`. The mock provider delivers once per key, and the tool is in `SIDE_EFFECTING`.
- **Regression:** control `notify_ignores_key`, seed 14.

#### P7: a run fails after its refund, without saying so

- **Discovery seed:** 14. Features: `io_error`, `scenario_faults`.
- **Schedule:** `issue_refund` succeeds, then `notify_customer` keeps failing until the run fails. The final state is `failed` with a refund in the ledger and no mention of it.
- **Root cause:** failing a run doesn't undo earlier steps, and the failure record didn't name what had committed.
- **Fix:** `2b96f5e`. `run_failed` lists `effects_committed`, and the UI shows them.
- **Regression:** control `hide_committed_effects`, seed 4.

## Part B: found by the simulator on its own

**One engine bug**, found during hardening once clients went through the HTTP layer. Before that, once the audit bugs were fixed, every failure the simulator reported was traced to the test apparatus. Those are listed too, because they're what it actually found.

### `POST /runs` answered 409 for a run it had created

- **Found:** while the HTTP layer was being added to the simulator, as `api_contract`. Nobody had predicted it.
- **Schedule:** a client's `POST /runs` creates the run and starts driving it. Another driver claims the run first (a sweep or an operator), and the create's own claim raises `Conflict`. The API turns that into 409, for a request that succeeded.
- **Why it matters:** a client with no idempotency key takes 409 to mean nothing happened, retries, and makes a second run. Calling the engine directly hid it, because there a conflict looked like any other.
- **Fix:** `15706ec`. `start_run_once` treats a conflict after creation as "created, someone else is driving it" and returns 201. `start_run` still raises, because stale-driver tests depend on it.
- **Regression:** control `create_reports_conflict`, seed 34, and an API test.

### Apparatus mistakes

| Finding | Seed | Where it was | Cause | Fix | Regression |
|---|---|---|---|---|---|
| `backoff_skipped` false positive | 2597 at `405d56b`, 4538 at `18061cb` | Checker | The checker timed a backoff from when the failure committed. The engine schedules it from a clock read just before that transaction. A jump between them, or just a slow commit, looked like a skip. The first fix (`e946ddc`) moved only the jump exemption and missed scenario faults, which raise before the tool seam. | `eb6cac7`: the checker now watches the clock read itself | `backoff_timed_from_commit`, seed 4538 |
| `read_path_writes` false positive | 11 at `9c99d58` | Checker | A reader that arrived before the schema existed began creating it, and was flagged once another process finished first | `c819d65` and `ada9639` (the hook landed in the second commit by mistake; the check is inert at `c819d65`) | `schema_judged_per_statement`, seed 121 |
| `read_path_writes` false positive on first connection | found while fixing P5 | Checker | The very first connection has to create the schema | `9c47eb9` | Covered by `test_sim_regressions.py` staying clean |
| Same-seed runs differed under pytest | n/a | Test fixture | An autouse fixture replaced the sleep seam, removing simulated sleep | Patch only the default runtime's sleep (`82507f0`) | `test_sim_determinism.py` |
| `two_drivers` false positive after power loss | 474, during Stage 2 | Checker | Leases restored from disk were timed from the restart, so a legitimate takeover looked early | Lease times kept per (run, token), and a restored lease keeps its first time. Fixed before the invariant was first committed, in `da8dc82` | `lease_timed_from_restart`, seed 1 |
| Undisclosed refund unchecked when notify failed | found while writing Stage 1 | Checker | A branch for runs that failed at `notify_customer` skipped the check for an undisclosed refund | Both checks run. Fixed before the change was committed, in `b0f8f61` | Covered by the reconciliation controls |
| `backoff_skipped` false positive after power loss | 2025602 and 2066987, first scheduled run at `1629a8f` | Checker | After a power loss the checker reads every surviving event again, and stamped each failed attempt with the restart time, so a retry soon after the restart looked early | A failure read again keeps the time it was first seen (`d4c7194`) | `test_sim_apparatus.py` |
| `backoff_skipped` by one microsecond | 2048818, same run | Simulator | The sleep seam truncated seconds to whole microseconds: 8,176 µs left of a backoff was slept as 8,175 | It rounds (`d4c7194`) | `test_sim_apparatus.py` |
| `two_drivers` false positive across a clock jump | 2935 at `d4c7194`, final survey | Checker | The engine reads the clock for a claim inside the claiming transaction. The checker expected a read before it, found none, and timed the holder's lease from its commit. That commit came after a real 68 s forward jump that had expired the 60 s lease, so a correct takeover looked early | A claim counts from when its transaction began, a takeover from its commit (`45e07e7`) | `lease_timed_from_commit`, seed 2935 |
| `stuck_run` with many poisoned runs | 39, during Stage 4 | Workload | The settling budget was a fixed number of rounds, and each poisoned run uses rounds of its own reaching its crash bound | The budget scales with the runs left (`f643707`) | Seed 39 now passes |

Two more problems came out of the negative controls rather than a survey:

- **Workload gap.** Operators picked only runs in a suitable status, so a decision on a finished run was almost never sent, though the API accepts one from any out-of-date page. The `decide_twice` control went uncaught for 300 seeds until `stale_operators` (`7d6c0dc`) was added.
- **Checker crash.** `terminal_not_final` produced a checkpoint over a step with no output, and the checker raised `TypeError` instead of reporting it. Fixed in `9af7ebf`.

### Survey of the fixed engine

`python -m aegisflow.sim survey --seeds 20000` at `eb6cac7`: seeds 0 to 19999, 1,535.2 s on a laptop with other jobs running. **No invariant fired.**

A clean range is evidence about those seeds, not proof that no bug remains.

## Part C: predicted weaknesses, confirmed before the fix

The previous report listed weak points: a provider that honours every key perfectly, manual recovery, unbounded crash loops, no HTTP in the simulator, and small workloads. The hardening work built the world those weak points describe, then the defences. None of these is a blind discovery. The weakness was written down first.

| Weak point | How it was confirmed | Result before the fix | Fix |
|---|---|---|---|
| The engine believed the provider's errors | Adversarial provider (false errors, stale replies) at `9bed504`, before reconciliation. `survey --seeds 2000`, 192.3 s | `undisclosed_effect_on_failed_step` in 29 seeds (first at seed 5), `notified_twice` in 3 (first at 296) | Reconciliation before retry or failure (`e6272bf`, `b0f8f61`) |
| Nothing recovered abandoned runs | `worker_does_nothing` control | `stuck_run` at seed 1 | Recovery worker (`8230e4b`) |
| Crash loops were unbounded | `no_crash_loop_bound` control | `crash_loop_unbounded` | Quarantine after 3 crashes (`da8dc82`) |

The last two rows are controls rather than a survey of the old engine. They show that the checker sees the problem, not how often the old code met it.

### Surveys during hardening

Each stage's survey covered seeds 0 to 2999 with every feature added so far. They ran on the working tree just before the stage was committed, so the commit column names the stage's last commit, not an exact tree that was surveyed:

| After | Stage committed as | Seconds | Result |
|---|---|---:|---|
| Stage 1, reconciliation | `e6272bf` to `acf644e` | 226.4 | clean |
| Stage 2, recovery and quarantine | `8230e4b` to `508e394` | 235.3 | clean |
| Stage 3, HTTP layer | `15706ec` to `f96bfd4` | 274.6 | clean, after the `15706ec` fix |

Stage 4's first 800 seeds with `large_workload` found `stuck_run` in 2 seeds. Both were the workload's settling budget (Part B), not the engine.

### The first scheduled long run

The nightly workflow's first scheduled run covered seeds 2,000,000 to 2,099,999 at `1629a8f`, before hardening, in 1,814.9 s, and failed 3 seeds, all on `backoff_skipped`. All three reproduced locally at that commit. Traces showed two apparatus mistakes (Part B), not an engine fault: in each, the engine waited out the stored backoff. With the fix (`d4c7194`) applied to `1629a8f`, all three seeds pass, both with their shrunk features and with all of them. At 3 seeds in this 100,000, the clean 20,000-seed survey and the hand-started 3,000-seed run aren't surprising. Both restart cases need a power loss less than one backoff before a retry. Finding them took the long run the workflow exists for.

The next two scheduled runs still tested `1629a8f`, because nothing had been pushed yet. Seeds 3,000,000 to 3,099,999 failed 1 (2,294.9 s), and 4,000,000 to 4,099,999 failed 4 (2,358.2 s), all on `backoff_skipped`. All five reproduced locally at `1629a8f` with the messages CI printed, and all five pass with the `d4c7194` fix applied there. Three were short by exactly 1 µs, the sleep-rounding mistake. For seed 4054315, rounding alone doesn't clear it, and the checker half of the fix does. Which half clears each of the other four wasn't separated.

### Survey at the end of hardening

`survey --seeds 4000` at `d4c7194` (seeds 0 to 3999, 626.7 s) fired one invariant on one seed: `two_drivers` on seed 2935. It was the checker (Part B), not the engine. The takeover followed a real forward clock jump. At `45e07e7`, with that fixed, the same range ran as `survey --seeds 2000 --start 0` and `--start 2000` (313.0 s and 318.4 s). **No invariant fired.**

## Negative controls

Each known bug class was put back, one at a time, as an exact source edit (`aegisflow/sim/mutations.py`), and seeds were run from 0 until a relevant invariant fired. Seeds run in order, so "seeds" is the count through the first catch.

### At `45e07e7`

From `python -m aegisflow.sim controls --max-seeds 3000` at `45e07e7`, 202 s. All 30 were caught. Every count is the same as in the run at `d4c7194`, before the checker's lease timing was fixed, so the fix cost no sensitivity on these seeds.

| Control | What it breaks | Caught by | Seeds |
|---|---|---|---:|
| `skip_idempotency_check` | the ledger ignores the idempotency key | `ledger_mismatch` | 6 |
| `skip_idempotency_check_and_cap` | the same, with the order cap also gone, so nothing stops a duplicate | `refund_cap` | 9 |
| `record_progress_first` | issue_refund is recorded as done before the refund is attempted | `succeeded_without_effect` | 2 |
| `drop_fsync` | commits are no longer synced, so power loss can erase acknowledged work | `ack_lost` | 2 |
| `no_fencing` | a driver whose lease was taken over keeps writing (P2) | `integrity_error` | 33 |
| `no_order_cap` | nothing limits total refunds to the order amount (P3) | `refund_cap` | 9 |
| `ignore_request_key` | POST /runs ignores the client's idempotency key (P3) | `request_ran_twice` | 27 |
| `lose_outcome_unknown` | a fatal error on a retried effect claims a known outcome (P1) | `unknown_reported_as_failure` | 440 |
| `hide_committed_effects` | a run failing after its refund doesn't say the refund happened (P7) | `undisclosed_effect_before_failure` | 5 |
| `volatile_backoff` | a takeover retries at once, ignoring the backoff still owed (P4) | `backoff_skipped` | 143 |
| `write_on_connect` | every connection, reads included, takes the write lock (P5) | `read_path_writes` | 3 |
| `notify_ignores_key` | the message provider delivers again on a repeated key (P6) | `notified_twice` | 15 |
| `create_reports_conflict` | POST /runs answers 409 for a run it created, if another driver takes it over | `api_contract` | 35 |
| `no_reconciliation` | nothing ever asks the provider what really happened | `ledger_mismatch` | 6 |
| `retry_before_reconcile` | a step is called again before the provider is asked | `notified_twice` | 13 |
| `unknown_as_failure` | what the lookup can't settle is reported as a plain failure | `unknown_reported_as_failure` | 13 |
| `trust_forgotten_keys` | no record is believed however old the key is | `notified_twice` | 13 |
| `no_crash_loop_bound` | a step that keeps killing its driver is resumed forever | `crash_loop_unbounded` | 8 |
| `recovery_without_fencing` | taking over a run doesn't raise its fencing epoch | `integrity_error` | 33 |
| `worker_ignores_lease` | the worker takes over runs whose drivers are still within their lease | `two_drivers` | 2 |
| `worker_does_nothing` | the sweep never picks up an abandoned run | `stuck_run` | 2 |
| `api_ignores_idempotency` | POST /runs drops the Idempotency-Key header before the engine sees it | `api_contract` | 28 |
| `no_backoff` | retries fire immediately | `backoff_skipped` | 1 |
| `unbounded_retries` | the attempt limit is ignored | `retry_limit` | 13 |
| `fresh_key_per_attempt` | each attempt uses a new idempotency key | `succeeded_without_notify` | 1 |
| `no_lease_exclusion` | any caller can claim a run another driver holds | `two_drivers` | 2 |
| `no_approval_gate` | refunds over the threshold skip approval | `gate_bypassed` | 2 |
| `split_progress_record` | a step's result and its event commit in separate transactions | `history_mismatch` | 3 |
| `decide_twice` | a decision applies even when no approval is pending | `double_decision` | 27 |
| `terminal_not_final` | advancing a failed run starts it going again | `terminal_changed` | 26 |

### At `eb6cac7`, before hardening

From `python -m aegisflow.sim controls --max-seeds 6000` at `eb6cac7`. All 20 were caught. The workload and checker have changed since, so these counts aren't comparable with the table above.

| Control | What it breaks | Caught by | Seeds |
|---|---|---|---:|
| `skip_idempotency_check` | the ledger ignores the idempotency key | `undisclosed_effect_on_failed_step` | 109 |
| `skip_idempotency_check_and_cap` | the same, with the order cap also gone, so nothing stops a duplicate | `refund_cap` | 9 |
| `record_progress_first` | issue_refund is recorded as done before the refund is attempted | `succeeded_without_effect` | 9 |
| `drop_fsync` | commits are no longer synced, so power loss can erase acknowledged work | `ack_lost` | 13 |
| `no_fencing` | a driver whose lease was taken over keeps writing (P2) | `history_mismatch` | 269 |
| `no_order_cap` | nothing limits total refunds to the order amount (P3) | `refund_cap` | 9 |
| `ignore_request_key` | POST /runs ignores the client's idempotency key (P3) | `request_ran_twice` | 15 |
| `lose_outcome_unknown` | a fatal error on a retried refund claims a known outcome (P1) | `undisclosed_effect_on_failed_step` | 286 |
| `hide_committed_effects` | a run failing after its refund doesn't say the refund happened (P7) | `undisclosed_effect_before_failure` | 6 |
| `volatile_backoff` | a takeover retries at once, ignoring the backoff still owed (P4) | `backoff_skipped` | 2,990 |
| `write_on_connect` | every connection, reads included, takes the write lock (P5) | `read_path_writes` | 3 |
| `notify_ignores_key` | the message provider delivers again on a repeated key (P6) | `notified_twice` | 20 |
| `no_backoff` | retries fire immediately | `backoff_skipped` | 1 |
| `unbounded_retries` | the attempt limit is ignored | `retry_limit` | 6 |
| `fresh_key_per_attempt` | each attempt uses a new idempotency key | `succeeded_without_notify` | 1 |
| `no_lease_exclusion` | any caller can claim a run another driver holds | `backoff_skipped` | 4,413 |
| `no_approval_gate` | refunds over the threshold skip approval | `gate_bypassed` | 8 |
| `split_progress_record` | a step's result and its event commit in separate transactions | `history_mismatch` | 4 |
| `decide_twice` | a decision applies even when no approval is pending | `double_decision` | 488 |
| `terminal_not_final` | advancing a failed run starts it going again | `terminal_changed` | 26 |

The three controls the brief named:

- **Skip the idempotency check** took 109 seeds, and it wasn't caught as a double refund. The order cap refused the duplicate money, so what surfaced was a run misreporting its outcome. With the cap removed too, it's caught as `refund_cap` in 9 seeds. Defences overlap, and the control needs both forms to show that.
- **Record progress before the side effect** took 9 seeds, as `succeeded_without_effect`.
- **Drop an fsync** took 13 seeds, as `ack_lost`. This tests the engine's declared sync level against the durability model, not a real disk.

Two other results are worth reading closely:

- **`fresh_key_per_attempt`** is caught at seed 1 only because the message goes out under a key the step never recorded. The cap hides its effect on refunds.
- **`no_lease_exclusion`** took 4,413 seeds, and was caught as a skipped backoff, with fencing still protecting the data. Before the checker fix in `eb6cac7`, it appeared to take 32, because most of those catches were the false positive above.
