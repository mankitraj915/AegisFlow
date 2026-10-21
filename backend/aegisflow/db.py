import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL,
    scenario_json TEXT NOT NULL,
    status        TEXT NOT NULL,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
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
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None: autocommit; transactions are explicit via tx().
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created before steps.attempt existed. Idempotent."""
    with tx(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(steps)")}
        if "attempt" not in cols:
            conn.execute("ALTER TABLE steps ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")


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
