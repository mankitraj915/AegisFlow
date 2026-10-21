# AegisFlow

Local-first runtime for deterministic, resumable, auditable refund workflow runs.
Python 3.11+, FastAPI, Pydantic v2, SQLite in `backend/`. React + TypeScript + Vite UI in `frontend/`. Deployed as one Render free service (`render.yaml`).

- `aegisflow/engine.py`: run state machine (`start_run`, `advance`, `approve`, `reject`) and reads.
- `aegisflow/planner.py`: pure next-step and approval-gate decisions over a fixed `PIPELINE`.
- `aegisflow/tools.py`: deterministic mock tools and data (`ORDERS`, `CUSTOMERS`); no external APIs.
- `aegisflow/db.py`: schema, migrations, `connect()`, `tx()`. `aegisflow/models.py`: Pydantic I/O models.
- `aegisflow/api.py`: thin HTTP layer over the engine. `create_app` is the API; `create_site` (production) mounts it at `/api` and serves `frontend/dist` at `/`. The UI always calls `/api`; in development, Vite proxies it to `:8000`.

## Commands (PowerShell, from `backend/`)

The `python` on PATH has no project dependencies; always use the venv interpreter.

    python -m venv .venv; .venv\Scripts\python.exe -m pip install -e ".[dev]"
    .venv\Scripts\python.exe -m pytest -q
    .venv\Scripts\python.exe -m pytest tests/test_retries.py -q
    .venv\Scripts\python.exe -m uvicorn aegisflow.api:create_app --factory

The API uses the SQLite file in `AEGISFLOW_DB` (default `aegisflow.db` in the current directory). Frontend, from `frontend/`: `npm run dev`; `npm run build` type-checks and builds.

## Invariants

- Only `engine.py` writes runs, steps, events, checkpoints and approvals. `tools.issue_refund` writes the `refunds` ledger in its own committed transaction, like an external system would.
- Connections autocommit; every write goes through `db.tx()` (`BEGIN IMMEDIATE`). The schema uses `CREATE TABLE IF NOT EXISTS`, so new columns on existing tables need an idempotent step in `db._migrate`.
- Money is integer paise everywhere, from the API to the ledger. Never use floats.
- Each step gets the idempotency key `{run_id}:{idx}` once. Retries and crash resumes reuse it. The ledger replays a known key, and a key reused with a different order or amount is a fatal error.
- `issue_refund` waits for approval if the amount is strictly greater than `approval_threshold_paise`, or if manual review is flagged on the scenario or the order. A decision only applies to a pending approval, so only one decider wins and runs `advance`.
- `ToolError` is fatal. `RetryableToolError` is retried under `engine.RETRY_POLICY` (deterministic capped exponential backoff, no jitter). Retries keep the step `started`, bump `steps.attempt`, and emit `step_attempt_failed`. Crashes don't use up attempts.
- Running out of retries, or a fatal error, fails the step and the run, and no later step runs. A side-effecting step that runs out records `idempotency_key` and `outcome_unknown: true`.
- Fault injection: `Scenario.faults` maps a step name to the outcome of each attempt (`retryable`, `fatal`, `retryable_after_commit`), applied by `tools.call`. The scenario is persisted with the run.
- Test seams: `hook(point, step_name)` at `after_tool` and `retry_scheduled` simulates crashes. The autouse `sleeps` fixture replaces `engine._sleep`, so tests never really sleep.
- No application path runs two `advance` calls on the same run, and direct concurrent callers are not guarded. Keep it that way, or add a guard with a test.

## Engineering standards

- Write idiomatic, readable code that matches the surrounding style and comment density.
- Add abstractions only when they remove real duplication or risk. Add dependencies only when unavoidable.
- Tests should check behavior and invariants (ledger rows, step and event history, terminal states), not implementation trivia.
- No speculative features, boilerplate, fake complexity, invented metrics, or claims the code and tests don't support.

## Workflow

- Read the relevant code before editing. Keep changes minimal and scoped, with no unrelated refactors.
- Never weaken or delete existing tests to make a change pass.
- After code changes, run the relevant tests, then the full suite, then `git diff --check`.
- Never commit, amend, or push without explicit authorization.

## Token efficiency

- Read targeted files and line ranges; avoid broad exploration and full-file dumps.
- Don't re-summarize known context, re-plan settled decisions, or spawn subagents for work you can do directly.
- Don't rerun tests that passed unless the code they cover has changed since.
