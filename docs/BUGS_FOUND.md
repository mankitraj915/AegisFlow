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

Schedules below come from `replay --seed <seed> --trace` at `005e746`, with the features the seed needs after shrinking. The regression seed is different from the discovery seed, because the workload has changed since. It's pinned in `backend/tests/test_sim_regressions.py`, where it must pass on the fixed code and break the named invariant with the bug put back.

#### P1: a committed refund, then a fatal error, reports plain `failed`

- **Discovery seed:** 256. Features: `duplicate_commands`, `tool_fatal`, `tool_late_delivery`, `tool_stall`.
- **Schedule:** on ORD-1001, the first `issue_refund` attempt fails with a retryable error and its effect isn't known to have been applied. The retry at 103.28 s gets a fatal error, so the run ends `failed`. The ledger holds a refund under that step's key, and the run's failure record doesn't mention it.
- **Root cause:** only retry exhaustion recorded `outcome_unknown`. The fatal path assumed a fatal error meant nothing had happened, which isn't true of a retry.
- **Fix:** `7db7bb2`. A fatal error on a side-effecting step that has already been attempted records the key and `outcome_unknown`.
- **Regression:** control `lose_outcome_unknown`, seed 285.

#### P2: a driver writes after losing its lease

- **Discovery seed:** 13 (`stale_write`). Features: `io_error`, `tool_stall`. The same cause showed up at seeds 551 and 2951 through forward clock jumps that expired a live lease.
- **Schedule:** `create-0` stalls in `fetch_order` at 0.04 s, past the 60 s lease. `operate-1` takes over at 70.9 s and fails on an I/O error. `create-0` wakes and writes `step_started` at 91.08 s.
- **Root cause:** a lease can expire under a driver that doesn't know, and nothing checked ownership at write time.
- **Fix:** `70e9855`. Each claim increments `runs.lease_epoch`, and every driver write checks it in the same transaction. [WALKTHROUGH.md](../WALKTHROUGH.md) has the reasoning.
- **Regression:** control `no_fencing`, seed 268, and `test_a_stale_driver_cannot_write_after_a_newer_one_took_over` for the exact symptom the audit probe showed.

#### P3: a repeated request refunds an order twice

- **Discovery seeds:** 3 (`refund_cap`, feature: `power_loss`) and 6 (`request_ran_twice`).
- **Schedule:** seed 3 has two separate requests for ORD-1001, so it shows the missing cap rather than a repeated request. A power loss at 0.46 s stops one run. At about 110.7 s an operator resumes it and approves the other, and both refund: 259,800 paise of 129,900. Seed 6 sends `req-0` twice, and it runs as two runs.
- **Root cause:** nothing tied a client request to a run, and nothing limited refunds across runs. Each run's key only stopped duplicates within that run.
- **Fix:** `ada9639` adds an optional `Idempotency-Key` on `POST /runs`, backed by a unique index. `0a09e56` adds a database trigger that refuses any refund past the order total. The tests that refunded one order twice were updated, and the demo data was reseeded with a pool of orders.
- **Regression:** controls `ignore_request_key`, seed 14, and `no_order_cap`, seed 8.

#### P4: a retry skips its backoff after a takeover

- **Discovery seed:** 1545. Features: `clock_jump`, `tool_delay`, `tool_drop_request`.
- **Schedule:** a forward clock jump at 0.2435 s expires `create-0`'s lease. Its `check_refund_policy` attempt then fails at 0.2462 s, and it commits the failure and sleeps 100 ms. `operate-3` takes over and retries 65 ms later. The jump came before the attempt, so it didn't shorten the backoff. The backoff was simply lost, because it lived only in the sleeping driver's memory.
- **Root cause:** the end of the backoff wasn't stored.
- **Fix:** `1290ae8`. `steps.next_attempt_at` stores it, and whoever picks the step up waits out the rest, capped at the policy's maximum in case the clock has jumped back.
- **Regression:** control `volatile_backoff`, seed 2989. This is the rarest control.

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
- **Regression:** control `notify_ignores_key`, seed 19.

#### P7: a run fails after its refund, without saying so

- **Discovery seed:** 14. Features: `io_error`, `scenario_faults`.
- **Schedule:** `issue_refund` succeeds, then `notify_customer` keeps failing until the run fails. The final state is `failed` with a refund in the ledger and no mention of it.
- **Root cause:** failing a run doesn't undo earlier steps, and the failure record didn't name what had committed.
- **Fix:** `2b96f5e`. `run_failed` lists `effects_committed`, and the UI shows them.
- **Regression:** control `hide_committed_effects`, seed 5.

## Part B: found by the simulator on its own

**No engine bugs.** Once the audit bugs were fixed, every failure the simulator reported was traced to the test apparatus. These are listed because they're what it actually found.

| Finding | Seed | Where it was | Cause | Fix | Regression |
|---|---|---|---|---|---|
| `backoff_skipped` false positive | 2597 at `405d56b`, 4538 at `18061cb` | Checker | The checker timed a backoff from when the failure committed. The engine schedules it from a clock read just before that transaction. A jump between them, or just a slow commit, looked like a skip. The first fix (`e946ddc`) moved only the jump exemption and missed scenario faults, which raise before the tool seam. | `eb6cac7`: the checker now watches the clock read itself | `backoff_timed_from_commit`, seed 4538 |
| `read_path_writes` false positive | 11 at `9c99d58` | Checker | A reader that arrived before the schema existed began creating it, and was flagged once another process finished first | `c819d65` and `ada9639` (the hook landed in the second commit by mistake; the check is inert at `c819d65`) | `schema_judged_per_statement`, seed 121 |
| `read_path_writes` false positive on first connection | found while fixing P5 | Checker | The very first connection has to create the schema | `9c47eb9` | Covered by `test_sim_regressions.py` staying clean |
| Same-seed runs differed under pytest | n/a | Test fixture | An autouse fixture replaced the sleep seam, removing simulated sleep | Patch only the default runtime's sleep (`82507f0`) | `test_sim_determinism.py` |

Two more problems came out of the negative controls rather than a survey:

- **Workload gap.** Operators picked only runs in a suitable status, so a decision on a finished run was almost never sent, though the API accepts one from any out-of-date page. The `decide_twice` control went uncaught for 300 seeds until `stale_operators` (`7d6c0dc`) was added.
- **Checker crash.** `terminal_not_final` produced a checkpoint over a step with no output, and the checker raised `TypeError` instead of reporting it. Fixed in `9af7ebf`.

### Survey of the fixed engine

`python -m aegisflow.sim survey --seeds 20000` at `eb6cac7`: seeds 0 to 19999, 1,535.2 s on a laptop with other jobs running. **No invariant fired.**

A clean range is evidence about those seeds, not proof that no bug remains.

## Negative controls

Each known bug class was put back, one at a time, as an exact source edit (`aegisflow/sim/mutations.py`), and seeds were run from 0 until a relevant invariant fired. From `python -m aegisflow.sim controls --max-seeds 6000` at `eb6cac7`. Seeds run in order, so "seeds" is the count through the first catch. All 20 were caught.

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
