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

The backend doesn't enable CORS. The Vite dev and preview servers proxy `/api/*` to `http://127.0.0.1:8000`.

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

To serve the production build instead, run `npm run build` and then `npm run preview`, which serves on http://localhost:4173 with the same proxy.

### API

| Method | Path | Purpose |
|---|---|---|
| POST | `/runs` | Create and run a workflow: `{order_id, scenario?}` |
| GET | `/runs` | List runs |
| GET | `/runs/{id}` | Run detail with steps and any pending approval |
| GET | `/runs/{id}/events` | Ordered event history |
| POST | `/runs/{id}/approve` | `{decided_by}` |
| POST | `/runs/{id}/reject` | `{decided_by, reason?}` |

Runs execute synchronously. `POST /runs` and `/approve` return only when the run has succeeded, failed, or paused for approval.

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

  The UI's "Fault injection" section provides presets for these.
- **Retries:** a step that fails with a transient error is retried up to 3 attempts in total, waiting 0.1s and then 0.2s between attempts. Every attempt reuses the step's idempotency key, so the refund ledger records each refund at most once.
- **Exhausted retries:** if `issue_refund` runs out of retries after its write may have committed, the run fails with `outcome_unknown: true` and the idempotency key in its failure events. The UI shows this as **Outcome unknown**.

## Tests

```powershell
cd backend
.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
npm run build   # type-check and production build
```

## Known limitations

- There is no authentication. `decided_by` is free text, and anyone who can reach the API can approve, reject or inject faults.
- There is no API to resume a run that stopped mid-step (status `running` after a crash), and no reconciliation for `outcome_unknown` runs.
- The run list shows `outcome_unknown` runs as `Failed`, because the list endpoint doesn't include events. The run detail view does distinguish them.
- The UI doesn't poll. Use Refresh or Reload to see changes made elsewhere.
- Orders, customers and the payment ledger are mocks. There are no external integrations.
- The frontend has no automated tests. It is checked by the TypeScript build and by manual or API-level end-to-end runs.
