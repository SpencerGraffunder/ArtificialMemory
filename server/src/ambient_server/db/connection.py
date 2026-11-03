"""SQLite connection management: WAL, sqlite-vec load, versioned schema runner."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger("ambient.db")

_SCHEMA_VERSION = 1

try:  # optional: ships as a wheel with the loadable extension bundled
    import sqlite_vec

    _SQLITE_VEC_AVAILABLE = True
except ImportError:  # pragma: no cover
    sqlite_vec = None  # type: ignore
    _SQLITE_VEC_AVAILABLE = False


def _connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    p = Path(path)
    if not readonly:
        p.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{p}?mode=ro" if readonly else f"file:{p}"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if _SQLITE_VEC_AVAILABLE:
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception as exc:  # noqa: BLE001 - any load failure degrades, never breaks
            log.warning("sqlite-vec extension unavailable (%s); using brute-force vectors", exc)
    return conn


def vec_extension_loaded(conn: sqlite3.Connection) -> bool:
    if not _SQLITE_VEC_AVAILABLE:
        return False
    row = conn.execute("SELECT vec_version()").fetchone()
    return row is not None


class Database:
    """One writer + one reader connection (WAL). Thread-safe via locks; safe to call
    from asyncio.to_thread worker threads."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._write_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._w = _connect(self.path)
        self._r = _connect(self.path)
        self.using_vec = vec_extension_loaded(self._w)

    # -- lifecycle ---------------------------------------------------------
    def migrate(self) -> None:
        with self._write_lock:
            cur = self._w.execute("PRAGMA user_version").fetchone()[0]
            if cur > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"db schema version {cur} is newer than server supports ({_SCHEMA_VERSION})"
                )
            schema = (Path(__file__).parent / "schema.sql").read_text()
            self._w.executescript(schema)
            self._w.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._w.commit()

    def close(self) -> None:
        with self._write_lock, self._read_lock:
            self._w.close()
            self._r.close()

    # -- access ------------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self._w.execute(sql, params)
            self._w.commit()
            return cur

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        with self._write_lock:
            self._w.executemany(sql, seq)
            self._w.commit()

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._read_lock:
            return self._r.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    class _Tx:
        def __init__(self, db: "Database"):
            self.db = db

        def __enter__(self) -> sqlite3.Connection:
            self.db._write_lock.acquire()
            self.db._w.execute("BEGIN IMMEDIATE")
            return self.db._w

        def __exit__(self, exc_type, exc, tb) -> None:
            try:
                if exc_type is None:
                    self.db._w.commit()
                else:
                    self.db._w.rollback()
            finally:
                self.db._write_lock.release()

    def transaction(self):
        """with db.transaction() as conn: conn.execute(...) — BEGIN IMMEDIATE, commit/rollback."""
        return Database._Tx(self)
