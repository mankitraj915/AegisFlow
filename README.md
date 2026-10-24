# AegisFlow

A local-first runtime for deterministic, resumable and auditable refund workflow runs, with a web UI for creating runs, looking at their history and making human approval decisions.

## Architecture

```
frontend/  React + TypeScript (Vite). Talks to the API through /api.
backend/   FastAPI + SQLite.
  aegisflow/api.py      HTTP endpoints
  aegisflow/engine.py   run state machine, retries, approvals, event log
  aegisflow/planner.py  fixed step pipeline and approval policy
  aegisflow/tools.py    deterministic mock tools, mock orders and refund ledger
  aegisflow/db.py       schema and migrations
```

Each run executes this pipeline: `fetch_order → fetch_customer → check_refund_policy → propose_refund → issue_refund → notify_customer`.

Runs, steps, events, checkpoints, approvals and the mock refund ledger are stored in one SQLite file. All amounts are integer paise.

The frontend always calls the API at `/api`, and the backend has no CORS:
- In development, the Vite dev and preview servers proxy `/api/*` to `http://127.0.0.1:8000`.
- In production, `create_site()` serves the API under `/api` and the built frontend at `/`, all from one process on one origin.

All workflow tools are simulated. Orders and customers are in-memory fixtures, the refund "payment system" is a table in the same SQLite file, and notifications only return the message text. Nothing makes a network call.

## Setup (Windows PowerShell)

You need Python 3.11+ and Node.js 20.19+.

```powershell
cd backend
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd ..\frontend
npm install
```

## Run locally

Use two terminals.

```powershell
# 1. API on http://127.0.0.1:8000 (database: $env:AEGISFLOW_DB, default .\aegisflow.db)
cd backend
.venv\Scripts\python.exe -m uvicorn aegisflow.api:create_app --factory --port 8000

# 2. UI on http://localhost:5173
cd frontend
npm run dev
```

To run it the way it is deployed (one process serving the API and UI), run this from the repo root:

```powershell
npm run build --prefix frontend
backend\.venv\Scripts\python.exe -m uvicorn aegisflow.api:create_site --factory --port 8000
# open http://127.0.0.1:8000
```

### Environment variables

All are optional.

| Variable | Default | Purpose |
|---|---|---|
| `AEGISFLOW_DB` | `aegisflow.db` (current directory) | SQLite file path |
| `AEGISFLOW_MAX_RUNS` | unset (unlimited) | Once this many runs exist, `POST /runs` returns 429 |
| `AEGISFLOW_FRONTEND_DIST` | `frontend/dist` in the source tree | Built UI served by `create_site` |

## Deploy (Render free tier, ₹0)

`render.yaml` defines a single free Python web service:
- **Build:** `pip install -e ./backend`, then `npm ci` and `npm run build` in `frontend/`. Render's native runtimes include Node.
- **Start:** `uvicorn aegisflow.api:create_site --factory --host 0.0.0.0 --port $PORT`
- **Health check:** `GET /api/health`
- **Settings:** pins `PYTHON_VERSION=3.11.15` and sets `AEGISFLOW_MAX_RUNS=500`.

To deploy:
1. Push this repository to GitHub (or GitLab/Bitbucket).
2. In the Render Dashboard, choose **New → Blueprint** and select the repository. Render reads `render.yaml`, creates the `aegisflow` service, and deploys every push to the default branch.

What the free tier means for this demo (see [Render's free tier documentation](https://render.com/docs/free)):
- **Data is temporary.** The filesystem is ephemeral, so the SQLite database is wiped on every deploy, restart and spin-down. That is the demo's reset behavior; nothing is meant to persist beyond the process.
- **Cold starts.** The service spins down after 15 minutes without traffic. The next visit waits about a minute while it starts again.
- **Small instance.** A single instance with 512 MB of memory.

### API

| Method | Path | Purpose |
|---|---|---|
| POST | `/runs` | Create and run a workflow: `{order_id, scenario?}` |
| GET | `/runs` | List runs |
| GET | `/runs/{id}` | Run detail with steps and any pending approval |
| GET | `/runs/{id}/events` | Ordered event history |
| GET | `/health` | Liveness and database check |
| POST | `/runs/{id}/approve` | `{decided_by}` |
| POST | `/runs/{id}/reject` | `{decided_by, reason?}` |
| POST | `/runs/{id}/resume` | Continue a run left `running` by an interruption (409 if it isn't `running` or is already being advanced) |

These paths are relative to the API root: `http://127.0.0.1:8000` with `create_app`, or `/api` with `create_site` and the dev proxy.

Runs execute synchronously. `POST /runs`, `/approve` and `/resume` return only when the run has succeeded, failed, paused for approval, or been interrupted (status `running`). `GET /runs/{id}` includes `refunds`, the ledger rows written under the run's idempotency keys.

Input limits:
- `order_id`: at most 64 characters
- `decided_by`: at most 100 characters
- `reason`: at most 500 characters
- each step's fault list: at most 10 entries

## Demo data

The mock orders are defined in `backend/aegisflow/tools.py`:

| Order | Amount | Behavior with default scenario |
|---|---|---|
| ORD-1001 | ₹1,299 | Completes without approval |
| ORD-2002 | ₹12,000 | Pauses for approval |
| ORD-3003 | ₹499 | Pauses for approval (order flagged for manual review) |
| ORD-4004 | ₹5,000 | Completes (the threshold check is strictly greater than) |
| ORD-9009 | cancelled | Fails: not refundable |

## Human approval

Before `issue_refund`, a run pauses with status `awaiting_approval` in either of these cases:
- the refund is strictly greater than `scenario.approval_threshold_paise` (default ₹5,000), or
- manual review is required by the scenario or by the order.

In the UI, open the run and enter who is deciding. Then:
- **Approve** resumes the run from its checkpoint and issues the refund. Earlier steps don't run again.
- **Reject** ends the run as `rejected`, and no refund is issued.

Only the first decision counts; any later one gets HTTP 409.

## Retries and fault injection

- **Fault injection:** `scenario.faults` maps a step name to the outcome of each attempt:
  - `retryable` fails that attempt with a transient error.
  - `fatal` fails the run without retrying.
  - `retryable_after_commit` runs the step, so the refund is written to the ledger, and then fails with a transient error.
  - `interrupt_after_commit` runs the step and then stops the run before the step is recorded as succeeded (see below).

  The UI's "Fault injection" section provides presets for these.
- **Retries:** a step that fails with a transient error is retried up to 3 attempts in total, waiting 0.1s and then 0.2s between attempts. Every attempt reuses the step's idempotency key, so the refund ledger records each refund at most once.
- **Exhausted retries:** if `issue_refund` runs out of retries after its write may have committed, the run fails with `outcome_unknown: true` and the idempotency key in its failure events. The UI shows this as **Outcome unknown**.

## Interruption and resume

The engine is re-entrant: a step left `started` is re-run with its original idempotency key, and the ledger replays a key it already has instead of writing a second refund. To make this visible, the `interrupt_after_commit` fault (UI preset *Simulated interruption after commit*) stops the run at one known point: after the step's tool call, so for `issue_refund` after the ledger write has committed, but before the step is recorded as succeeded. It is the same window the crash tests cover.

The request that hit the interruption returns normally with status `running`, and the run records a `run_interrupted` event with the step and key. Run responses also carry `interrupted`, derived from the events: true while the run is `running` and its latest interrupt or resume event is `run_interrupted`. The status itself stays `running`; the UI shows such runs as **Interrupted**. Nothing else happens until someone resumes it: `POST /runs/{id}/resume`, or **Resume run** in the UI. That records `run_resumed`, re-runs the step with the same key and attempt number, and finishes the run. The run detail then shows one ledger row under that key, and the step history has no duplicate step. The interruption fires once per step, so the resumed attempt completes.

This is a simulated stop at a fixed boundary inside a live process. It does not show the service surviving a real process restart: on Render's free tier the SQLite file is ephemeral, so a restart deletes the run along with everything else.

Only one `advance` can drive a run at a time. Starting, approving and resuming each take a lease on the run (`runs.lease_token`) in a write transaction and release it when advancing stops, including on errors. A second caller gets HTTP 409. If a process is killed mid-run, its lease expires after 60 seconds, which is far longer than a run takes, and the run can then be resumed.

## Tests

```powershell
cd backend
.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
npm run build   # type-check and production build
```

## Known limitations

- **This is a public, shared simulation.** There is no authentication: `decided_by` is free text, and any visitor can create, approve or reject any run, or inject faults. The UI says so in a banner, and the run cap limits how much data visitors can create.
- Data lasts only as long as the server process (see Deploy).
- Resume is manual. Nothing finds and resumes `running` runs automatically after a crash, and there is no reconciliation for `outcome_unknown` runs.
- The run list shows `outcome_unknown` runs as `Failed`, because the list endpoint doesn't include events. The run detail view does distinguish them.
- The UI doesn't poll. Use Refresh or Reload to see changes made elsewhere.
- Orders, customers and the payment ledger are mocks. There are no external integrations.
- The frontend has no automated tests in the repository. It is checked by the TypeScript build and by manual end-to-end runs against the real backend.
