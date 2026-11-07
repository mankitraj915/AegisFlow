import time

from aegisflow import engine
from aegisflow.db import connect
from aegisflow.models import Scenario


def test_reads_do_not_wait_for_the_write_lock(db_path):
    writer = connect(db_path)
    run_id = engine.start_run(writer, "ORD-1001", Scenario())
    writer.execute("BEGIN IMMEDIATE")
    started = time.perf_counter()
    reader = connect(db_path)  # Used to block for the 10 s busy timeout, then raise.
    assert engine.get_run(reader, run_id).status == "succeeded"
    assert time.perf_counter() - started < 5
    reader.close()
    writer.execute("ROLLBACK")
    writer.close()


def test_schema_is_created_once_and_survives_reconnecting(db_path):
    connect(db_path).close()
    conn = connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] > 0
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    conn.close()
