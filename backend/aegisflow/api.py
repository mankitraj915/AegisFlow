import os

from fastapi import Depends, FastAPI, HTTPException

from . import engine
from .db import connect
from .models import CreateRun, Decision, EventOut, RunDetail, RunOut


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("AEGISFLOW_DB", "aegisflow.db")
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

    @app.post("/runs", response_model=RunOut, status_code=201)
    def create_run(body: CreateRun, conn=Depends(get_conn)):
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

    @app.post("/runs/{run_id}/reject", response_model=RunOut)
    def reject(run_id: str, body: Decision, conn=Depends(get_conn)):
        guarded(engine.reject, conn, run_id, body.decided_by, body.reason)
        return engine.get_run(conn, run_id)

    return app
