"""The engine's connection to the outside world.

Everything the engine can't decide for itself comes through `current`: time,
identifiers, waiting, opening a database, and calling an external system.
Production uses the real implementations below. The simulator (aegisflow.sim)
installs controlled ones, so the same engine code runs inside a seeded world and
every failure replays from its seed. tests/test_seams.py fails if engine code
reaches past this module.

Process death has no seam of its own: a simulated process dies at whichever of
these calls it makes next, which covers every point where the engine touches
anything outside its own memory.
"""
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def _open_db(path: str) -> Any:
    # isolation_level=None: autocommit; transactions are explicit via db.tx().
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _invoke(name: str, key: str, call: Callable[[], dict]) -> dict:
    return call()


@dataclass
class Runtime:
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    new_id: Callable[[], str] = lambda: uuid.uuid4().hex
    sleep: Callable[[float], None] = time.sleep
    open_db: Callable[[str], Any] = _open_db
    # Wraps each tool call: invoke(tool name, idempotency key, call the tool).
    invoke: Callable[[str, str, Callable[[], dict]], dict] = _invoke
    # Where refunds go, given the engine's connection. None: that same database.
    ledger: Callable[[Any], Any] | None = None
    # Delivers a customer message. Nothing is sent in production: this is a mock.
    send_message: Callable[[str, str], None] = lambda to, message: None


current = Runtime()


@contextmanager
def use(runtime: Runtime) -> Iterator[Runtime]:
    global current
    previous, current = current, runtime
    try:
        yield runtime
    finally:
        current = previous
