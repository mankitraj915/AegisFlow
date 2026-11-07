"""Real SQLite under simulated faults.

Each simulated process gets a real stdlib connection to a real WAL database, so
isolation and recovery are SQLite's own. SimConn adds a seam before every
statement, turns lock contention into simulated waiting, and injects I/O errors.

Durability is modelled, not observed: stdlib sqlite3 has no hook on fsync. From
SQLite's documentation, in WAL mode synchronous=FULL syncs the WAL at each commit
and OFF never does. So `DurableWal` tracks the WAL offset that would have reached
the disk under the sync level the engine asked for, and a power loss keeps only
some prefix of the WAL at or beyond it. The real connection runs with sync off,
for speed. Auto-checkpointing is off, so checkpoint crashes and torn pages in the
main database file are not covered.
"""
import os
import re
import sqlite3
from typing import Any

_LOCKED = re.compile(r"locked|busy", re.IGNORECASE)
_SYNC_PRAGMA = re.compile(r"^\s*PRAGMA\s+synchronous\s*=\s*(\w+)", re.IGNORECASE)
WRITE_VERBS = {"BEGIN", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "SCRIPT"}
BUSY_TIMEOUT_US = 10_000_000  # Matches the production connection's timeout=10.


def raw_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("PRAGMA synchronous=OFF")
    return conn


class SimCursor:
    """Fully fetched, so no read statement stays open across a seam."""

    def __init__(self, rows: list, rowcount: int):
        self._rows, self.rowcount = rows, rowcount

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class SimConn:
    def __init__(self, world: Any, proc: Any):
        self.world, self.proc = world, proc
        self.raw = raw_connect(world.path)
        proc.conns.append(self)

    def _with_busy_wait(self, run):
        sched = self.world.sched
        deadline = sched.true_us + BUSY_TIMEOUT_US
        while True:
            try:
                return run()
            except sqlite3.OperationalError as exc:
                if not _LOCKED.search(str(exc)) or sched.true_us >= deadline:
                    raise
                sched.sleep_us(sched.rng.randrange(500, 5000))

    def execute(self, sql: str, params: tuple = ()) -> SimCursor:
        world = self.world
        verb = sql.split(None, 1)[0].upper()
        world.sched.step(f"sql {verb}")
        world.note_statement(self.proc, verb)
        if verb == "PRAGMA":
            match = _SYNC_PRAGMA.match(sql)
            if match:
                world.wal.declared = match.group(1).upper()
                return SimCursor([], -1)
        elif verb == "ROLLBACK":
            if self.raw.in_transaction:
                self.raw.execute("ROLLBACK")
            return SimCursor([], -1)
        elif verb == "COMMIT":
            return self._commit()
        elif world.on("io_error"):
            raise sqlite3.OperationalError("disk I/O error (simulated)")
        cursor = self._with_busy_wait(lambda: self.raw.execute(sql, params))
        rows = cursor.fetchall() if cursor.description else []
        return SimCursor(rows, cursor.rowcount)

    def _commit(self) -> SimCursor:
        world = self.world
        if world.on("commit_lost"):
            self.raw.execute("ROLLBACK")
            raise sqlite3.OperationalError("disk I/O error during commit, not applied (simulated)")
        before = world.wal.durable
        self.raw.execute("COMMIT")
        if world.on("torn_commit"):
            world.power_loss(torn_from=before)
        world.after_commit(self.proc)
        if world.on("commit_unknown"):
            raise sqlite3.OperationalError("disk I/O error during commit, but applied (simulated)")
        return SimCursor([], -1)

    def executescript(self, script: str) -> None:
        self.world.sched.step("sql SCRIPT")
        self.world.note_statement(self.proc, "SCRIPT")
        self._with_busy_wait(lambda: self.raw.executescript(script))
        self.world.after_commit(self.proc)

    def close(self) -> None:
        # Not a seam: a dying process's connection closes without its help.
        self.raw.close()


class DurableWal:
    """What survives power loss, per the sync level the engine asked for."""

    def __init__(self, path: str):
        self.path, self.wal_path = path, path + "-wal"
        self.declared = "FULL"  # SQLite's default until a PRAGMA says otherwise.
        self.durable = 0

    def size(self) -> int:
        return os.path.getsize(self.wal_path) if os.path.exists(self.wal_path) else 0

    def committed(self) -> None:
        if self.declared in ("FULL", "EXTRA", "2", "3"):
            self.durable = self.size()

    def image(self, rng: Any, torn_from: int | None) -> tuple[bytes, bytes]:
        """The files as a power loss would leave them. torn_from: the commit just
        made was still reaching the disk, so the cut falls somewhere inside it."""
        size = self.size()
        low = self.durable if torn_from is None else torn_from
        cut = rng.randint(min(low, size), size)
        with open(self.path, "rb") as main:
            db = main.read()
        with open(self.wal_path, "rb") if size else open(os.devnull, "rb") as wal:
            wal_bytes = wal.read(cut)
        return db, wal_bytes

    def restore(self, image: tuple[bytes, bytes]) -> None:
        db, wal = image
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)
        with open(self.path, "wb") as main:
            main.write(db)
        if wal:
            with open(self.wal_path, "wb") as out:
                out.write(wal)
        self.durable = len(wal)
