import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.staticfiles import StaticFiles

from . import engine, recovery, tools
from .db import connect
from .models import AvailableOrder, CreateRun, Decision, EventOut, Resolution, RunDetail, RunOut

# Built frontend, relative to the source tree (backend/aegisflow -> frontend/dist).
DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


logger = logging.getLogger("aegisflow.recovery")
DEFAULT_RECOVERY_INTERVAL_S = 30.0


def _database(db_path: str | None) -> str:
    return db_path or os.environ.get("AEGISFLOW_DB", "aegisflow.db")


def _recovery_loop(path: str, stop: threading.Event, interval_s: float) -> None:
    while not stop.wait(interval_s):
        try:
            conn = connect(path)
            try:
                result = recovery.sweep(conn)
            finally:
                conn.close()
            for run_id, exc in result.errors:
                logger.error("recovering run %s failed: %r", run_id, exc)
        except Exception:
            logger.exception("recovery sweep failed")  # The next sweep starts afresh.


def _recovering(path: str):
    """Lifespan that sweeps for abandoned runs in the background while the server is
    up. AEGISFLOW_RECOVERY_INTERVAL_S sets how often; 0 turns it off."""
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        interval_s = float(os.environ.get("AEGISFLOW_RECOVERY_INTERVAL_S", DEFAULT_RECOVERY_INTERVAL_S))
        stop = threading.Event()
        worker = None
        if interval_s > 0:
            worker = threading.Thread(target=_recovery_loop, args=(path, stop, interval_s), daemon=True)
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                worker.join(5)

    return lifespan


def create_app(db_path: str | None = None, max_runs: int | None = None) -> FastAPI:
    path = _database(db_path)
    if max_runs is None and os.environ.get("AEGISFLOW_MAX_RUNS"):
        max_runs = int(os.environ["AEGISFLOW_MAX_RUNS"])
    connect(path).close()  # create schema up front
    app = FastAPI(title="AegisFlow", lifespan=_recovering(path))

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
            raise HTTPException(404, "run not found") from None
        except engine.OrderNotFound:
            raise HTTPException(404, "order not found") from None
        except engine.Conflict as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/health")
    def health(conn=Depends(get_conn)):
        conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/orders/available", response_model=list[AvailableOrder])
    def available_orders(conn=Depends(get_conn)):
        return tools.available_orders(conn)

    @app.post("/runs", response_model=RunOut, status_code=201)
    def create_run(
        body: CreateRun,
        response: Response,
        idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None,
        conn=Depends(get_conn),
    ):
        replay = idempotency_key is not None and engine.run_id_for_key(conn, idempotency_key) is not None
        # Bounds database growth on a shared public demo; unset means unlimited.
        if not replay and max_runs is not None and engine.count_runs(conn) >= max_runs:
            raise HTTPException(429, f"This demo is limited to {max_runs} runs. It resets when the server restarts.")
        run_id, created = guarded(engine.start_run_once, conn, body.order_id, body.scenario, idempotency_key)
        if not created:
            response.status_code = 200  # A repeat of an earlier request: the same run, nothing new.
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

    @app.post("/runs/{run_id}/reconcile", response_model=RunOut)
    def reconcile(run_id: str, conn=Depends(get_conn)):
        guarded(engine.reconcile, conn, run_id)
        return engine.get_run(conn, run_id)

    @app.post("/runs/{run_id}/resolve", response_model=RunOut)
    def resolve(run_id: str, body: Resolution, conn=Depends(get_conn)):
        guarded(engine.resolve, conn, run_id, body.decided_by, body.outcome)
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
    # A mounted app's lifespan doesn't run, so the site is the one that sweeps.
    site = FastAPI(title="AegisFlow", docs_url=None, redoc_url=None, openapi_url=None,
                   lifespan=_recovering(_database(db_path)))
    site.mount("/api", create_app(db_path))
    site.mount("/", StaticFiles(directory=dist, html=True))
    return site
