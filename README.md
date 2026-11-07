# AegisFlow

A small workflow runtime that issues refunds through six recorded steps and stays correct when things go wrong, tested by a deterministic simulator that runs the real engine through crashes, power loss, I/O errors, lost and duplicated messages and clock jumps, and replays any failure from its seed.

**Live demo:** https://aegisflow-075f.onrender.com (shared simulation on Render's free tier; the first visit may take about a minute to wake the service)

![An interrupted run: steps 0–3 succeeded, issue_refund interrupted after its ledger write, with the idempotency key and a Resume action](docs/screenshots/interrupted-run.png)

*A run stopped right after `issue_refund` wrote to the simulated ledger and before the step was recorded. Resuming re-runs the step with the same key; the ledger replays it instead of writing a second refund.*

The refunds, orders, payment ledger and notifications are all simulated. Nothing calls an external system, and no LLM is involved: the pipeline and its decisions are deterministic.

## The claim, and how it's tested

Under crashes at any seam, power loss, I/O errors, lost, delayed, duplicated and late messages, and clock jumps:

- each step's side effect is applied at most once, and an order is never refunded past its total;
- a finished run stays finished and is never re-run from scratch;
- a driver that has been replaced can't write;
- retries respect their limit and their backoff;
- durable state matches the event history;
- acknowledged work survives power loss, and a failed run says which effects committed.

This is systematic testing, not a formal proof. The evidence:

- **A deterministic simulator** (`backend/aegisflow/sim/`). The real engine runs on baton-passing threads against real SQLite, and a seed decides the interleaving, the clock and the faults. The same seed gives the same run, byte for byte.
- **A history checker** with 29 named invariants, which shares no logic with the engine.
- **Negative controls.** 20 known bugs, put back as exact source edits. Each is caught within a stated number of seeds.
- **Regression tests** pinning a seed per bug that passes on the fixed code and fails with the bug put back.

**Latest results:** 20,000 seeds with no invariant violated (seeds 0 to 19999, commit `eb6cac7`), and all 20 controls caught, the slowest taking 4,413 seeds. Those are results for those seeds, not a guarantee about all of them.

[WALKTHROUGH.md](WALKTHROUGH.md) explains how this works and where it stops. [STUDY.md](STUDY.md) has questions and answers by stage.

### Bugs found

The audit found these by hand first. The simulator then re-found each one on the unfixed engine, with `survey --seeds 6000` at commit `005e746`. **After those fixes, it found no further engine bugs.** The failures it reported afterwards were mistakes in the checker. Details, schedules and the control results are in [docs/BUGS_FOUND.md](docs/BUGS_FOUND.md).

| Audit | Bug | Seeds to re-find | Fix |
|---|---|---:|---|
| P5 | Opening a connection takes the write lock, even to read | 3 | `4213f93` |
| P3 | A repeated request refunds an order twice | 4 | `0a09e56` |
| P3 | The same request runs as two runs | 7 | `ada9639` |
| P6 | `notify_customer` delivers twice after a crash | 4 | `9c99d58` |
| P2 | A driver writes after losing its lease | 14 | `70e9855` |
| P7 | A run fails after its refund committed, without saying so | 15 | `2b96f5e` |
| P1 | A committed refund, then a fatal error, reports plain `failed` | 257 | `7db7bb2` |
| P4 | A retry skips its backoff after a crash or takeover | 1,546 | `1290ae8` |

### Run the simulator

```powershell
cd backend
.venv\Scripts\python.exe -m aegisflow.sim run --seeds 1000             # add --budget-seconds N to stop on time
.venv\Scripts\python.exe -m aegisflow.sim replay --seed 42 --trace     # one seed, every scheduling step
.venv\Scripts\python.exe -m aegisflow.sim survey --seeds 5000          # seeds each invariant takes to break
.venv\Scripts\python.exe -m aegisflow.sim controls                     # put known bugs back, count seeds to catch
```

A failing seed prints a minimal `reproduce:` command. CI runs up to 5,000 seeds on a time budget on every push, and a nightly workflow covers a new seed range and can be started by hand for longer runs.

## What it demonstrates

The interesting part is the window between "the payment system committed" and "the workflow recorded that it committed". A crash, timeout or retry in that window is where duplicate payments come from. AegisFlow handles it like this:

- **Stable idempotency keys.** Step *n* of a run always uses `{run_id}:{n}`. Retries and resumes reuse it. The ledger (written in its own transaction, as an external payment system would) replays a key it has already seen, and a reused key with a different order or amount is a fatal error.
- **Re-entrant execution.** A step left `started` is simply run again under its key. Earlier completed steps are never repeated.
- **Bounded, deterministic retries.** Transient faults retry up to 3 attempts in total (0.1 s, then 0.2 s; no jitter). The backoff is stored with the step, so whoever picks the step up waits out the rest of it. Fatal faults fail the run immediately. If a side-effecting step fails when its effect may have committed, the run fails with `outcome_unknown: true` and the key, instead of claiming success or failure. A run that fails later names the effects already committed.
- **Human approval.** Refunds strictly above the scenario threshold (default ₹5,000), or flagged for manual review, pause before `issue_refund`. Only the first decision applies; a second one gets HTTP 409.
- **Single driver per run.** Starting, approving and resuming take a lease on the run inside a write transaction. A concurrent caller gets HTTP 409. A lease left by a killed process expires after 60 seconds. Expiry alone can't stop a stalled driver from carrying on, so each claim also increments a fencing token, and every write checks it in the same transaction.
- **One run per request, one refund up to the total.** An optional `Idempotency-Key` on `POST /runs` maps a client request to one run. A database trigger refuses any refund that takes an order past its total.
- **Audit history.** Every transition is appended to an ordered event log, shown in the UI next to the ledger rows written under the run's keys.

![Scenario presets and the six-step pipeline](docs/screenshots/scenarios.png)

### Execution of one step

```
step n ──► record step_started, key = {run_id}:{n}
        ──► tool call  (issue_refund commits its ledger row here, under the key)
        ──►   ◄── crash / interruption / timeout window ──►
        ──► record step_succeeded, checkpoint, move to step n+1

Retry or resume inside the window: same key ─► ledger replays the existing row ─► one refund
```

## Recovery experiment

You can reproduce three levels of failure, from simulated to real.

1. **Simulated interruption (UI or API).** Choose *Interruption and resume* and start the run. The `interrupt_after_commit` fault stops the run after `issue_refund`'s ledger write and before the step is recorded. The run stays `running`, records `run_interrupted` with the key, and the UI shows **Interrupted** with a **Resume run** button. Resuming (`POST /runs/{id}/resume`) records `run_resumed`, re-runs step 4 with the same key and attempt number, and finishes with one ledger row. This is a stop at a fixed point inside a live process, not a restart.
2. **Retry after commit.** The *Retry after commit* preset makes the refund commit and then time out once. The retry reuses the key and the ledger still has one row.
3. **Real process death (test).** `test_resume_after_real_process_death` in [`backend/tests/test_recovery.py`](backend/tests/test_recovery.py) starts a run in a child Python process, waits until it has committed the refund, and kills it (`TerminateProcess`/`SIGKILL`, so no cleanup runs). From a fresh connection the test then checks:
   - exactly one refund, step 4 still `started`, and the dead process's lease still held, so `resume` is refused with a conflict;
   - after the lease expiry time has passed (the test advances the engine's clock past 60 s rather than sleeping), `resume` succeeds, reuses the same key, leaves one refund, and produces the exact expected event sequence.

Recovery after process death needs two things: the SQLite file must survive, and someone must call resume after the lease expires. Nothing resumes runs automatically.

## Run it locally (Windows PowerShell)

You need Python 3.11+ and Node.js 20.19+.

```powershell
cd backend
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd ..\frontend
npm install
```

Development, in two terminals:

```powershell
# API on http://127.0.0.1:8000 (SQLite file: $env:AEGISFLOW_DB, default .\aegisflow.db)
cd backend
.venv\Scripts\python.exe -m uvicorn aegisflow.api:create_app --factory --port 8000

# UI on http://localhost:5173 (proxies /api to :8000)
cd frontend
npm run dev
```

As deployed, one process serving the API and the built UI, from the repo root:

```powershell
npm run build --prefix frontend
backend\.venv\Scripts\python.exe -m uvicorn aegisflow.api:create_site --factory --port 8000
# open http://127.0.0.1:8000
```

### Tests

```powershell
cd backend
.venv\Scripts\python.exe -m pytest -q   # engine, retries, recovery, API and simulator tests
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy

cd ..\frontend
npm run build                            # type-check and production build
```

The backend tests check behaviour and invariants: ledger rows, step and event history, terminal states, lease conflicts, concurrent approvals, migration of an older database, and the crash windows above. Tests never really sleep; backoff is replaced by a fixture. `tests/test_seams.py` fails if engine code reaches time, randomness, storage or the network other than through `aegisflow/runtime.py`.

## Architecture

```
 React UI ──► api.py ──► engine.py ──► planner.py     pure next-step and approval decisions
 (Vite)       HTTP         │ state machine, retries, leases and fencing, event log
                           ▼
                       runtime.py   the seams: clock, ids, sleep, open_db, invoke, ledger
                        │       │
          production    │       │    simulator (aegisflow/sim/)
          ──────────    │       │    ─────────────────────────
          real clock    │       │    scheduler.py  seeded baton-passing, simulated time
          sqlite3 file ◄┘       └──► storage.py    real SQLite, crash images, I/O faults
          tools.py ledger            world.py      faults, external system, clients
                                     workload.py   what each seed asks for
                                     checker.py    29 invariants, watching from outside
                                     mutations.py  known bugs, as exact source edits
```

- `aegisflow/engine.py`: run state machine, retries, approvals, leases, event log
- `aegisflow/planner.py`: fixed six-step pipeline and approval policy
- `aegisflow/tools.py`: deterministic mock tools and refund ledger; `demo_data.py`: the order pool
- `aegisflow/db.py`: schema and migrations
- `aegisflow/api.py`: HTTP layer; `create_site` serves the API at `/api` and the UI at `/`

Pipeline: `fetch_order → fetch_customer → check_refund_policy → propose_refund → issue_refund → notify_customer`.

Runs, steps, events, checkpoints, approvals and the refund ledger live in one SQLite file. Money is integer paise everywhere. The `checkpoints` table holds audit snapshots; recovery rebuilds state from steps and doesn't read it.

### API

Paths are relative to the API root: `http://127.0.0.1:8000` with `create_app`, or `/api` with `create_site` and the dev proxy.

| Method | Path | Purpose |
|---|---|---|
| POST | `/runs` | Create and run a workflow: `{order_id, scenario?}`. With an `Idempotency-Key` header, a repeat returns the same run with 200, and the key on a different request gets 409 |
| GET | `/orders/available` | The next order of each kind with something left to refund |
| GET | `/runs` | List runs |
| GET | `/runs/{id}` | Run detail with steps, pending approval and ledger rows (`refunds`) |
| GET | `/runs/{id}/events` | Ordered event history |
| POST | `/runs/{id}/approve` | `{decided_by}` |
| POST | `/runs/{id}/reject` | `{decided_by, reason?}` |
| POST | `/runs/{id}/resume` | Continue an interrupted `running` run (409 if not `running` or already being advanced) |
| GET | `/health` | Liveness and database check |

Runs execute synchronously: `POST /runs`, `/approve` and `/resume` return once the run has finished, failed, paused for approval or been interrupted.

A scenario sets `approval_threshold_paise`, `require_manual_review` and `faults`, which maps a step name to the outcome of each attempt: `retryable`, `fatal`, `retryable_after_commit` or `interrupt_after_commit`.

### Demo orders

An order can be refunded only up to its total, so the demo seeds a pool of each kind. The UI picks the next usable one.

| Orders | Amount | Default scenario |
|---|---|---|
| ORD-1001 to ORD-1500 | ₹1,299 | Completes without approval |
| ORD-2002 to ORD-2101 | ₹12,000 | Pauses for approval |
| ORD-3003 to ORD-3052 | ₹499 | Pauses for approval (order flagged for manual review) |
| ORD-4004 to ORD-4053 | ₹5,000 | Completes (the threshold is strictly greater than) |
| ORD-9009 | cancelled | Fails the policy check |

## Deployment

[`render.yaml`](render.yaml) defines one free Render web service. It builds the backend and frontend, starts `uvicorn aegisflow.api:create_site`, health-checks `/api/health`, and caps the demo at 500 runs (`AEGISFLOW_MAX_RUNS`; further `POST /runs` return 429). Every push to `master` redeploys it.

| Variable | Default | Purpose |
|---|---|---|
| `AEGISFLOW_DB` | `aegisflow.db` in the current directory | SQLite file path |
| `AEGISFLOW_MAX_RUNS` | unlimited | Run cap for the public demo |
| `AEGISFLOW_FRONTEND_DIST` | `frontend/dist` in the source tree | Built UI served by `create_site` |

## Limitations

- **Fsync behaviour is modelled, not observed.** Stdlib `sqlite3` has no hook on fsync, so the simulator derives what would survive power loss from SQLite's documentation of `synchronous=FULL` in WAL mode. It doesn't test whether a disk honours a sync.
- **WAL checkpoint crashes are not covered.** Auto-checkpointing is off in the simulator, so crashes during a checkpoint and torn pages in the main database file are outside what's tested.
- **A clean run isn't a proof.** Results hold for the fault model, the workload and the seeds reported. The invariants and the engine share an author, and threads take turns, so real parallelism is tested only by the process-death test.
- **Wall-clock backoff.** A forward clock jump can shorten a backoff.
- **Crash loops are unbounded.** Crashes don't use up retry attempts, by design.
- **Simulated integrations.** Orders, customers, the payment ledger and notifications are local mocks in the same SQLite file. There are no real payments, messages or external APIs.
- **Ephemeral public demo.** Render's free tier has no persistent disk, so the database is wiped on every deploy, restart and idle spin-down. The live demo does not demonstrate recovery across a restart; the process-death recovery is shown by the test against a surviving local database.
- **Manual recovery.** Nothing scans for and resumes stuck runs, and there is no reconciliation for `outcome_unknown` runs.
- **Shared, unauthenticated demo.** Anyone can create, approve, reject or resume any run; `decided_by` is free text.
- **Single SQLite process.** The lease guards concurrent callers on one database file. This is not a distributed system, and there are no performance or scale measurements.
- **UI gaps.** The run list shows `outcome_unknown` runs as `Failed` (the list endpoint carries no events), the UI doesn't poll, and the frontend has no automated tests in the repository; it is checked by the TypeScript build and by browser runs against a local backend.
