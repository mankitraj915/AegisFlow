# AegisFlow audit

Audited at commit `4bd42a0`, the current `master` and `origin/master`. I read all the Python, all the tests, the README, `render.yaml` and the git history, plus the frontend except `styles.css` (presentational). The unmodified code was probed with throwaway scripts. Every result below comes from those runs, the test suite, or a named line of code. Anything else is marked as read or unmeasured.

**Summary.** AegisFlow handles the failure points it names, and its tests prove those points. Its strongest claim is broader than that evidence: "retries and resumes never write a second refund" has only been tested at a handful of hand-picked points. Probing found three behaviours that contradict the documented claims, and the suite has 98% branch coverage with all three present. Correctness under failure is mostly asserted, because no test explores failure beyond those points.

## 1. What it actually does

AegisFlow runs a fixed six-step refund pipeline synchronously inside an HTTP request:

`fetch_order → fetch_customer → check_refund_policy → propose_refund → issue_refund → notify_customer`

- **Execution model.** `POST /runs`, `/approve` and `/resume` call `engine.advance`, which drives the run until it finishes, fails, pauses for approval or is interrupted. There is no queue, no background worker and no timer service, and nothing resumes a stuck run. The only timers are a blocking `time.sleep` for backoff and a lease expiry timestamp.
- **State.** The source of truth is the `steps` table. `_load_state` (`engine.py:97`) rebuilds state from succeeded steps. `events` is an audit log the engine does not read to recover. `checkpoints` is written but never used for recovery: the only read is `MAX(after_step_idx)`, for display (`engine.py:400`).
- **Side effects.** `issue_refund` writes a `refunds` row in its own transaction, in the *same SQLite file* as the engine, and "external system" is a convention. `notify_customer` composes a message and sends nothing.
- **Concurrency.** A lease on the run row (`_claim`, `engine.py:108`) is taken in a write transaction, with a 60 s expiry and no renewal.
- **Stack.** Python 3.11, FastAPI, Pydantic v2, SQLite 3.53 (WAL, `synchronous=2` by default, measured). React and TypeScript UI. One free Render service.

## 2. Correctness claims and their evidence

Evidence levels:

- **Proven:** a test exercises the claim and would fail if it broke.
- **Partial:** proven at named points only, with the general claim untested.
- **Asserted:** stated in docs or comments, with no test.
- **Contradicted:** a probe shows behaviour that disagrees with the claim.

| # | Claim | Level | Evidence |
|---|---|---|---|
| 1 | Each step has one stable key, reused by retries and resumes | Proven | `test_failure_after_refund_commit_retries_with_same_key`, `test_crash_after_refund_commit_on_retry_attempt`, `test_resume_after_real_process_death` |
| 2 | Retries and resumes never write a second refund *within a run* | Partial | Tested at the `after_tool` and `retry_scheduled` hooks, `retryable_after_commit`, and one real kill. No test crashes at an arbitrary statement or commit boundary |
| 3 | A refund is recorded at most once for a request | **Contradicted** | P3: a repeated `POST /runs` refunds ₹2,598 on a ₹1,299 order. The README lead and the UI (`App.tsx:55`) read as if this can't happen |
| 4 | The ledger replays a known key and rejects a mismatched one | Proven | `test_refund_ledger_is_idempotent_and_rejects_payload_mismatch`, which calls the tool directly |
| 5 | Completed steps are never repeated | Partial | `test_approve_resumes_without_rerunning_earlier_steps`, and crash tests at the named hooks |
| 6 | Retry count is bounded | Proven | `test_retry_exhaustion_fails_run_and_stops`, `test_retry_policy_bounds_attempts`. Crashes are deliberately not counted, so crash loops are unbounded (P8) |
| 7 | Backoff delays are respected | Partial | Delay values are proven in an uninterrupted run. They are not persisted, and P4 shows an exception during backoff lets the next attempt run without waiting |
| 8 | A run with an uncertain refund outcome says so (`outcome_unknown`) | **Contradicted** | Proven for retry exhaustion only. P1: commit, then a fatal error on the retry, gives plain `failed` with a refund in the ledger and no marker |
| 9 | Approval threshold and manual-review gate | Proven | `test_engine.py`: threshold boundary, order flag, scenario flag |
| 10 | Only one decider wins | Proven | `test_concurrent_approvals_have_one_winner`, using 4 real threads, so its interleaving is not controlled |
| 11 | At most one `advance` drives a run | **Contradicted** after expiry | Proven while the lease is live. P2: nothing fences a driver whose lease lapsed, so it overwrites another driver's completed run |
| 12 | "`LEASE_S` is far longer than any advance takes" | Asserted | Docstring only (`engine.py:22`). There is no renewal, and true only of mock tools |
| 13 | A lease left by a killed process expires, then the run recovers | Proven at one point | `test_resume_after_real_process_death` really kills a process, at one point, with the clock patched |
| 14 | The event log is a faithful, ordered history | Partial | Sequence numbers are checked gapless. Nothing checks that state matches events, and P2 produces a history with both `run_succeeded` and `run_failed` |
| 15 | Committed state survives a crash | Asserted | Process kill only, which does not lose OS-cached writes. `synchronous` is an unset default. Power loss and torn writes are untested |
| 16 | Execution is deterministic | Partial | Tests compare outputs after removing the run id. Timestamps and UUIDs are real, so runs are not byte-reproducible |
| 17 | Money is integer paise everywhere | Partial | `test_money_is_integer_paise_throughout` covers one path. The column has no type constraint |
| 18 | Every step is safe to retry | Partial | True of the mocks. `notify_customer` ignores its key and is not in `SIDE_EFFECTING`, and it runs twice after a crash (P6). A real email would be sent twice |
| 19 | Older databases migrate | Proven | `test_milestone1_database_is_migrated_and_resumable` |

### Probe results

All probes ran against unmodified code. To keep the simulator's findings genuine, none of these is fixed yet.

| Probe | Setup | Result | Reachable today? |
|---|---|---|---|
| **P1** | `issue_refund: ["retryable_after_commit", "fatal"]` | Run `failed`, 1 ledger row, `run_failed` has no `outcome_unknown` or key. The UI shows plain "Failed" | Yes, via the public API |
| **P2** | Lease lapses during a driver's tool call. A second driver finishes the run, then the first gets a tool error | Status goes `succeeded → failed`, step 5 goes `succeeded → failed`, and events hold both `run_succeeded` and `run_failed`. Cause: `_fail` writes unconditionally (`engine.py:265`) | Only with a stall over 60 s or a forward clock jump, since mock tools take microseconds. Real tools would make it easy |
| **P3** | The same `POST /runs` body sent twice | 2 ledger rows, ₹2,598 refunded against a ₹1,299 order | Yes. A client retry after a lost response is all it takes |
| **P4** | Exception during backoff, then immediate `advance` | The next attempt runs with no wait. No column stores the next attempt time | After an exception, which releases the lease. A real kill leaves the 60 s lease, which outlasts the backoff |
| **P5** | Write lock held, then `connect()` | Raises `database is locked` after 11.27 s. Every request, including `GET /health`, takes the write lock in `_migrate` (`db.py:88`) | Yes, though write transactions are normally short |
| **P6** | Crash after `notify_customer`, then resume | Tool runs twice | Yes, with no visible effect because nothing is sent |
| **P7** | `notify_customer: ["fatal"]` | Run `failed` with the refund committed and no marker that money moved | Yes |
| **P8** | 21 crashes at `after_tool` | `attempt` stays 1, 1 ledger row, still `running`. Safe, but nothing bounds it | By design |
| **P9** | Search for reads of `checkpoints` | Only the display `MAX()` | n/a |

Branch coverage is **98%** (engine 99%), and P1, P2, P3 and P4 all exist alongside it. The only uncovered engine branches are `217->222`, `250->172` and `297->306`. The first two are the guards written for a concurrent or stale driver, so that code has never run under test.

## 3. Shallow, duplicated or fragile code

- **No fencing.** The lease controls entry only. After `_claim`, no write checks the token. Only the conditional `UPDATE`s at `engine.py:212` and `:245` protect anything, and `_fail` and `_set_status` have no such condition.
- **Nondeterminism is scattered and only partly seamed:** `db.now` (read by `engine` and `tools`), `datetime.now` directly in `_claim` (`engine.py:111`), `uuid.uuid4` (`:110`, `:137`), and `time.sleep` (`:55`). Only the sleep has a named seam. Lease expiry reads two different clocks, which is why `test_resume_after_real_process_death` can only fake half the comparison.
- **Exception "crashes" are not kills.** All but one crash test raises from a hook, so `_drive`'s `finally` releases the lease, which a real kill would not do. Those tests are exercising a different recovery path from the one they appear to model.
- **Ledger and engine share a database.** They would lose data together, so the divergence that makes the commit window dangerous can't occur on storage loss. It also means `get_run_detail` can `JOIN` across a boundary that a real external system wouldn't allow.
- **Checkpoints are decorative.** They are written transactionally but never used for recovery. Either use them or say so.
- **Fault injection is in production paths.** `Scenario.faults` is split across `tools.call` and `engine._interrupt_planned`, and anyone can set it through the public API. That suits a demo but is not a fault model.
- **Reads inside `_advance` run outside any transaction.** That is safe only if the lease is exclusive, which P2 disproves.
- **Timestamps compare as strings.** Lease expiry relies on `isoformat()` ordering. It happens to be correct: for a timestamp with zero microseconds, `+` sorts before `.`. It is untested and easy to break.
- **`max_runs` is count-then-insert** (`api.py:47`), so concurrent requests can exceed the cap. Read, not run.
- **Duplication.** `PIPELINE` is hardcoded in two test files and the frontend, and the `refunds`/`steps` test helpers are repeated across files.
- **Stale metadata.** `pyproject.toml` calls this an "AI operations runtime" with "agent runs", while the README correctly says no LLM is involved.

## 4. Test and tooling gaps

- No crash testing at arbitrary points, and no controlled interleavings. The two concurrency tests use real threads.
- No I/O faults, partial writes, clock jumps or duplicated requests anywhere.
- No test compares durable state with event history.
- **No CI of any kind.** `.github/` doesn't exist and `gh run list` is empty.
- **No linter or type checker** is configured for the backend. The Stage gates will need ruff and mypy (both free).
- No frontend tests. The README discloses this. `npm run build` was not run for this audit, though Render's successful build at this commit shows it built there.
- A deprecation warning on every test run: Starlette wants `httpx2` in place of `httpx` for its test client.

## 5. Deploy status

Checked read-only on 2026-11-07. I sent no `POST` requests to the live service.

| Check | Result |
|---|---|
| Latest GitHub deployment | `4bd42a0` (equal to local `HEAD` and `origin/master`), state `success`, 2026-10-25 |
| `GET /` | 200 in 31.4 s, a cold start |
| `GET /api/health` | 200 in 0.34 s |
| `GET /api/runs` | `[]`, consistent with the documented reset on spin-down |

The live demo works but can't demonstrate recovery, because its disk is ephemeral. The README says so.

## 6. What an interviewer would poke at first

1. **"My client timed out and retried `POST /runs`. How many refunds?"** Two (P3). This is the first thing a payments engineer would ask.
2. **"What happens when a lease expires under a live worker?"** It gets overwritten (P2). The expected follow-up is "where's your fencing token?"
3. **"You say exactly once?"** It is at-least-once execution plus an idempotent receiver, and the receiver here is a table in your own database. Could the step and the ledger row just share a transaction? Yes, so the window exists by convention.
4. **"What are the checkpoints for?"** Currently, nothing recovery depends on.
5. **"Who resumes a stuck run?"** Nobody, unless a person clicks a button. It is durable state, not durable execution.
6. **"You killed the process. Did you lose power?"** No. A process kill doesn't lose OS-cached writes, so fsync has never been tested.
7. **"You have 98% coverage, so what?"** Coverage shows the lines ran, not that they survive any failure ordering.
8. **"Is `notify_customer` exactly-once?"** No, and its key is unused.
9. **"Why does a failed run have a refund in the ledger?"** P1 and P7.
10. **"Who can approve a refund?"** Anyone. `decided_by` is free text and the demo has no auth. The README discloses this.

## 7. Proposed transformation

### Recommendation: A with B's checker as its oracle

Build the deterministic simulator, and make the history checker the way it judges each run. The two need each other:

- **A alone** can drive every failure ordering, but checking only final rows would miss bugs. P2's final state is internally consistent; only the history shows `succeeded` becoming `failed`, and a lost commit shows up only against what was acknowledged.
- **B alone** on real threads can't reach the crash windows on demand or replay what it finds, which is what makes a failure usable.

Stages 1 to 4 of the brief already describe this combination, and the audit supports it: the gaps above are about unexplored failure orderings, which is what A explores, and the contradictions are about history and acknowledgements, which is what B checks.

### Ranking

| Rank | Option | Verdict |
|---|---|---|
| 1 | **A + B as oracle** | Recommended. Reproducible search over failures, with a checker that sees history |
| 2 | A alone | Weaker oracle. Misses history-only bugs such as P2's terminal flip |
| 3 | B alone (real threads and kills) | Not reproducible, hits windows by luck, and can't satisfy "same seed, same run" |
| 4 | C, replay with versioning | **Rejected.** See below |

### Rejected alternatives

- **C, event-sourced replay and versioning.** It solves a problem AegisFlow doesn't have. The pipeline is one fixed list, state comes from `steps`, and there is no user workflow code to replay. It would mean rewriting the engine and rerunning all of its proofs, and it would prove nothing about the bugs above. One real limitation remains: `idx = COUNT(*)` makes changing `PIPELINE` unsafe for in-flight runs. Document that instead.
- **Rewriting the engine with async or generators to get yield points.** It would make the tested code different from the shipped code. The real synchronous engine runs unchanged on OS threads, with a baton so exactly one runs at a time. Yields happen at the seams.
- **Hypothesis stateful testing as the driver.** It has good shrinking, but its example database and search aren't a stable seed-to-run mapping. A small purpose-built seeded generator satisfies the brief more directly.
- **Jepsen, TLA+ or P.** Jepsen's JVM cluster setup doesn't fit 8 GB or this single-file system, and a spec model doesn't test the shipped code.

### How the brief maps onto what exists

Some faults in the brief have no direct equivalent here, so I'd map them as follows:

| Brief says | Maps to |
|---|---|
| Workers | Concurrent API callers: `start_run`, `approve`, `reject`, `resume`. There are no worker processes |
| Timers | Backoff sleeps and lease expiry |
| Delayed or duplicated messages | Duplicated or delayed API commands, plus tool calls whose request is repeated or whose response is lost. There is no message bus |
| Process crash | A true kill: the next seam raises, the connection closes uncommitted, and `finally` blocks can't write. So unlike the existing hook crashes, the lease is left behind |
| I/O errors | Errors at the storage seam, including a commit that succeeded but reported failure |
| Partial writes, dropped fsync | Crash images (below) |
| External system | The ledger becomes a seam. Production keeps today's table and behaviour; the simulator uses a separate store that survives engine storage loss, so divergence becomes observable |
| Operator | The harness plays the person calling `resume`. I would not add an auto-recovery worker, which would be a new feature. This stays a documented limitation unless you want it |

### Storage model: measured options

| Option | Result on this machine | Verdict |
|---|---|---|
| **Stdlib `sqlite3` on a temp-file WAL, with crash images** | Copy the db plus a WAL cut at any byte offset. Cuts at a commit boundary, mid-frame, and one byte short of a commit all recovered correctly with `integrity_check` ok (3, 3, 3, 4 and 5 rows as expected). 12.5 ms median per plain run | **Recommended.** Same driver as production and no new dependency |
| APSW Python VFS | Works, and `xSync` calls are directly observable, but there are no Python-level shared-memory methods and it is a different driver and SQLite build (3.54 vs 3.53) | Rejected as the main path. Possibly a single calibration test confirming the fsync assumption |
| `memdb` VFS | 1.7 ms per run, but `journal_mode=memory`, not WAL, so isolation differs from production | Rejected. It would test different semantics |

**Limits of the recommended storage model, which the final docs must state:**

- Durability is *modelled from SQLite's documented sync behaviour*: a commit is durable at `FULL`, and not at `OFF`. Stdlib SQLite can't let me observe the fsync itself.
- Auto-checkpointing stays off so that the WAL holds all recent writes. Crashes during a checkpoint, and torn pages in the main database file, are therefore not covered.
- This mostly tests AegisFlow's configuration and assumptions, not SQLite. Finding a SQLite bug is not a goal.

### Bugs I already know about

P1, P2, P3 and P4 came from reading the code and probing, **not from the simulator**. If the simulator rediscovers them, `docs/BUGS_FOUND.md` will say exactly that, separately from anything it finds on its own. If it can't reach one, that is a finding about the simulator. They also serve as ready-made checks that its search works.

### Decisions taken

1. **Transformation:** A with B as the oracle, as recommended.
2. **"Exactly once" scope:** an optional idempotency key on `POST /runs`, **and** a database-enforced cap so that total refunds never exceed the order amount. This overrides the audit's recommendation against a cap: tests that encode the old behaviour get updated and the demo orders get reseeded. "Refunds never exceed the order amount" is also a checker invariant.
3. **Storage model:** stdlib WAL crash images. The README and WALKTHROUGH must state that fsync behaviour is modelled from SQLite's documentation and that checkpoint crashes aren't covered.
4. **Lease expiry:** fix with fencing tokens, so a stale driver can never write after a newer one takes over.
5. **Provenance:** `BUGS_FOUND.md` separates audit-probe bugs from bugs the simulator found on its own. Before any audit bug is fixed, the simulator must find it independently, with the seed count recorded.

### Risks

- **Throughput is unmeasured.** A plain run takes 12.5 ms, but thread hand-offs, fault logic and checking will add to that. Seed counts will come from real runs, and the CI seed count will be set from a time budget.
- **Memory.** This machine had about 1.1 to 1.3 GB free during the audit, out of 8 GB. Runs will be bounded and sequential, and each seed's temp files deleted.
- **Pushing CI.** The `gh` token lacks the `workflow` scope, and `git` uses Git Credential Manager, whose token scope I can't see. A push adding `.github/workflows` may be refused until the scope is granted.
