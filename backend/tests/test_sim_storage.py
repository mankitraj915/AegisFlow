"""The crash-image model against real SQLite recovery."""
import random
import sqlite3

import pytest

from aegisflow.sim.storage import DurableWal, raw_connect


@pytest.fixture
def wal_db(tmp_path):
    path = str(tmp_path / "w.db")
    conn = raw_connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    wal = DurableWal(path)
    wal.committed()
    yield path, conn, wal
    conn.close()


def recovered_rows(tmp_path, wal, image):
    target = DurableWal(str(tmp_path / "image.db"))
    target.restore(image)
    conn = sqlite3.connect(target.path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return [r[0] for r in conn.execute("SELECT x FROM t ORDER BY x")]
    finally:
        conn.close()


def test_power_loss_keeps_every_commit_when_each_was_synced(tmp_path, wal_db):
    path, conn, wal = wal_db
    for i in range(5):
        conn.execute("INSERT INTO t VALUES (?)", (i,))
        wal.committed()
    for seed in range(5):
        assert recovered_rows(tmp_path, wal, wal.image(random.Random(seed), None)) == list(range(5))


def test_power_loss_can_lose_unsynced_commits_but_keeps_a_prefix(tmp_path, wal_db):
    path, conn, wal = wal_db
    wal.declared = "OFF"
    for i in range(5):
        conn.execute("INSERT INTO t VALUES (?)", (i,))
        wal.committed()
    outcomes = {tuple(recovered_rows(tmp_path, wal, wal.image(random.Random(seed), None))) for seed in range(40)}
    assert () in outcomes or (0,) in outcomes  # Something was lost.
    assert all(rows == tuple(range(len(rows))) for rows in outcomes)  # Never a hole.


def test_a_torn_commit_recovers_to_the_commit_before_it(tmp_path, wal_db):
    path, conn, wal = wal_db
    conn.execute("INSERT INTO t VALUES (0)")
    wal.committed()
    before = wal.durable
    conn.execute("INSERT INTO t VALUES (1)")  # In flight: never reported as synced.
    outcomes = {tuple(recovered_rows(tmp_path, wal, wal.image(random.Random(seed), before))) for seed in range(40)}
    assert outcomes <= {(0,), (0, 1)} and (0,) in outcomes
