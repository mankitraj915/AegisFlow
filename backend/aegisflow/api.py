import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from . import engine
from .db import connect
from .models import CreateRun, Decision, EventOut, RunDetail, RunOut

# Built frontend, relative to the source tree (backend/aegisflow -> frontend/dist).
DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(db_path: str | None = None, max_runs: int | None = None) -> FastAPI:
    path = db_path or os.environ.get("AEGISFLOW_DB", "aegisflow.db")
    if max_runs is None and os.environ.get("AEGISFLOW_MAX_RUNS"):
        max_runs = int(os.environ["AEGISFLOW_MAX_RUNS"])
    connect(path).close()  # create schema up front
    app = FastAPI(title="AegisFlow")

    def get_conn():
        conn = connect(path)
        try:
            yield conn
        finally:
            conn.close()

    def guarded(fn, *args):
        try:
            return fn(*args)
        except engine.RunNotFound:
            raise HTTPException(404, "run not found")
        except engine.OrderNotFound:
            raise HTTPException(404, "order not found")
        except engine.Conflict as exc:
            raise HTTPException(409, str(exc))

    @app.get("/health")
    def health(conn=Depends(get_conn)):
        conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.post("/runs", response_model=RunOut, status_code=201)
    def create_run(body: CreateRun, conn=Depends(get_conn)):
        # Bounds database growth on a shared public demo; unset means unlimited.
        if max_runs is not None and engine.count_runs(conn) >= max_runs:
            raise HTTPException(429, f"This demo is limited to {max_runs} runs. It resets when the server restarts.")
        run_id = guarded(engine.start_run, conn, body.order_id, body.scenario)
        return engine.get_run(conn, run_id)

    @app.get("/runs", response_model=list[RunOut])
    def list_runs(conn=Depends(get_conn)):
        return engine.list_runs(conn)

    @app.get("/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: str, conn=Depends(get_conn)):
        return guarded(engine.get_run_detail, conn, run_id)

    @app.get("/runs/{run_id}/events", response_model=list[EventOut])
    def get_events(run_id: str, conn=Depends(get_conn)):
        return guarded(engine.list_events, conn, run_id)

    @app.post("/runs/{run_id}/approve", response_model=RunOut)
    def approve(run_id: str, body: Decision, conn=Depends(get_conn)):
        guarded(engine.approve, conn, run_id, body.decided_by)
        return engine.get_run(conn, run_id)

    @app.post("/runs/{run_id}/resume", response_model=RunOut)
    def resume(run_id: str, conn=Depends(get_conn)):
        guarded(engine.resume, conn, run_id)
        return engine.get_run(conn, run_id)

    @app.post("/runs/{run_id}/reject", response_model=RunOut)
    def reject(run_id: str, body: Decision, conn=Depends(get_conn)):
        guarded(engine.reject, conn, run_id, body.decided_by, body.reason)
        return engine.get_run(conn, run_id)

    return app


def create_site(db_path: str | None = None, frontend_dist: str | None = None) -> FastAPI:
    """Production entry point: the API under /api and the built frontend at /, on one origin.
    Matches the paths the Vite dev proxy uses, so no CORS is needed."""
    dist = Path(frontend_dist or os.environ.get("AEGISFLOW_FRONTEND_DIST") or DEFAULT_FRONTEND_DIST)
    if not (dist / "index.html").is_file():
        raise RuntimeError(f"frontend build not found at {dist}; run `npm run build` in frontend/")
    site = FastAPI(title="AegisFlow", docs_url=None, redoc_url=None, openapi_url=None)
    site.mount("/api", create_app(db_path))
    site.mount("/", StaticFiles(directory=dist, html=True))
    return site
