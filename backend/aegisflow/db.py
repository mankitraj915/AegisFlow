import sqlite3
from contextlib import contextmanager

from . import runtime

SYNCHRONOUS = "FULL"
SCHEMA_VERSION = 5 # Raise whenever _migrate gains a step.

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL,
    scenario_json TEXT NOT NULL,
    status        TEXT NOT NULL,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    -- Held while one advance() drives the run; see engine._claim.
    lease_token      TEXT,
    lease_expires_at TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    id              INTEGER PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    idx             INTEGER NOT NULL,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL,
    input_json      TEXT NOT NULL,
    output_json     TEXT,
    error           TEXT,
    attempt         INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    UNIQUE (run_id, idx)
);
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(id),
    seq          INTEGER NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts           TEXT NOT NULL,
    UNIQUE (run_id, seq)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES runs(id),
    after_step_idx INTEGER NOT NULL,
    state_json     TEXT NOT NULL,
    ts             TEXT NOT NULL,
    UNIQUE (run_id, after_step_idx)
);
CREATE TABLE IF NOT EXISTS approvals (
    id                     INTEGER PRIMARY KEY,
    run_id                 TEXT NOT NULL REFERENCES runs(id),
    step_idx               INTEGER NOT NULL,
    status                 TEXT NOT NULL,
    requested_payload_json TEXT NOT NULL,
    decided_by             TEXT,
    reason                 TEXT,
    decided_at             TEXT,
    UNIQUE (run_id, step_idx)
);
-- Mock external payment system. Amounts are integer paise.
CREATE TABLE IF NOT EXISTS refunds (
    idempotency_key TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL CHECK (amount_paise > 0),
    created_at      TEXT NOT NULL
);
-- Mock message provider: one delivery per key. Nothing is actually sent.
CREATE TABLE IF NOT EXISTS notifications (
    idempotency_key TEXT PRIMARY KEY,
    recipient       TEXT NOT NULL,
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def now() -> str:
    return runtime.current.clock().isoformat()


def connect(path: str) -> sqlite3.Connection:
    conn = runtime.current.open_db(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    # Sync the WAL on every commit, so a commit survives power loss. SQLite's
    # usual default, stated here because the durability claims depend on it.
    conn.execute(f"PRAGMA synchronous={SYNCHRONOUS}")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema. Skipped once current, so an ordinary
    connection, including every read, never needs the write lock."""
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created before steps.attempt or the runs lease existed. Idempotent."""
    with tx(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(steps)")}
        if "attempt" not in cols:
            conn.execute("ALTER TABLE steps ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
        run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        for col in ("lease_token", "lease_expires_at"):
            if col not in run_cols:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
        if "lease_epoch" not in run_cols:
            # Fencing token: see engine._fenced_tx.
            conn.execute("ALTER TABLE runs ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0")
        if "next_attempt_at" not in cols:
            # When a step's backoff ends, so it survives whoever was sleeping through it.
            conn.execute("ALTER TABLE steps ADD COLUMN next_attempt_at TEXT")
        if "idempotency_key" not in run_cols:
            # One run per client request: see engine.start_run_once.
            conn.execute("ALTER TABLE runs ADD COLUMN idempotency_key TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS runs_idempotency_key ON runs(idempotency_key) "
            "WHERE idempotency_key IS NOT NULL"
        )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


@contextmanager
def tx(conn: sqlite3.Connection):
    """Write transaction. BEGIN IMMEDIATE takes the write lock up front so
    read-then-write sequences inside (e.g. event seq allocation) are atomic."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
