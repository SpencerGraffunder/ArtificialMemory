"""Vector storage with two backends: sqlite-vec (preferred) and brute-force float32 blobs.

Both expose the same interface. The brute-force fallback scans only the candidate ids
given to `search` (or everything when candidates is None) with numpy-free math; for a
single-user store (<100k memories) this is well under the 100 ms L1 budget.
"""

from __future__ import annotations

import array
import math
import logging
from typing import Protocol

from .connection import Database

log = logging.getLogger("ambient.db")


def pack_embedding(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()  # native little-endian on all supported platforms


def unpack_embedding(blob: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


class VectorIndex(Protocol):
    def upsert(self, row_id: str, vec: list[float]) -> None: ...
    def delete(self, row_id: str) -> None: ...
    def search(
        self, query: list[float], k: int, candidates: set[str] | None = None
    ) -> dict[str, float]:
        """Return {id: cosine_similarity} for top-k."""
        ...


class BruteForceIndex:
    """Fallback: embeddings kept in `<table>` (id, embedding BLOB)."""

    def __init__(self, db: Database, table: str):
        assert table in ("memory_vectors_fb", "utterance_vectors_fb")
        self._db = db
        self._table = table
        self._id_col = "memory_id" if table == "memory_vectors_fb" else "utterance_id"

    def upsert(self, row_id: str, vec: list[float]) -> None:
        self._db.execute(
            f"INSERT INTO {self._table} ({self._id_col}, embedding) VALUES (?, ?) "
            f"ON CONFLICT({self._id_col}) DO UPDATE SET embedding=excluded.embedding",
            (row_id, pack_embedding(vec)),
        )

    def delete(self, row_id: str) -> None:
        self._db.execute(f"DELETE FROM {self._table} WHERE {self._id_col}=?", (row_id,))

    def search(
        self, query: list[float], k: int, candidates: set[str] | None = None
    ) -> dict[str, float]:
        if candidates is not None and not candidates:
            return {}
        if candidates is not None:
            marks = ",".join("?" * len(candidates))
            rows = self._db.query(
                f"SELECT {self._id_col} AS id, embedding FROM {self._table} "
                f"WHERE {self._id_col} IN ({marks})",
                tuple(candidates),
            )
        else:
            rows = self._db.query(f"SELECT {self._id_col} AS id, embedding FROM {self._table}")
        scored = ((r["id"], cosine(query, unpack_embedding(r["embedding"]))) for r in rows)
        top = sorted(scored, key=lambda t: t[1], reverse=True)[:k]
        return dict(top)


class SqliteVecIndex:
    """sqlite-vec virtual table (TEXT primary key, float32 dims).

    Metric portability: older sqlite-vec builds (incl. the v0.1.9 wheel) compute L2 and
    silently ignore `distance_metric=cosine`. We therefore store L2-NORMALIZED vectors
    and convert: for unit vectors L2² = 2(1−cos) ⇒ cos = 1 − d²/2 — identical semantics
    to the brute-force backend on any vec version."""

    def __init__(self, db: Database, table: str, dim: int):
        self._db = db
        self._table = table
        self._dim = dim
        db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
            f"{_VEC_ID_COL[table]} TEXT PRIMARY KEY, embedding float[{dim}])"
        )

    def upsert(self, row_id: str, vec: list[float]) -> None:
        assert len(vec) == self._dim, f"embedding dim {len(vec)} != index dim {self._dim}"
        id_col = _VEC_ID_COL[self._table]
        self._db.execute(
            f"INSERT OR REPLACE INTO {self._table} ({id_col}, embedding) VALUES (?, ?)",
            (row_id, pack_embedding(normalize(vec))),
        )

    def delete(self, row_id: str) -> None:
        self._db.execute(
            f"DELETE FROM {self._table} WHERE {_VEC_ID_COL[self._table]}=?", (row_id,)
        )

    def search(
        self, query: list[float], k: int, candidates: set[str] | None = None
    ) -> dict[str, float]:
        if candidates is not None and not candidates:
            return {}
        id_col = _VEC_ID_COL[self._table]
        sql = (
            f"SELECT {id_col} AS id, distance FROM {self._table} "
            f"WHERE embedding MATCH ? AND k = ?"
        )
        params: tuple = (pack_embedding(normalize(query)), k)
        if candidates is not None:
            marks = ",".join("?" * len(candidates))
            sql += f" AND {id_col} IN ({marks})"
            params += tuple(candidates)
        try:
            rows = self._db.query(sql, params)
        except Exception:  # noqa: BLE001 - vec0 k constraint quirks → fallback set
            return BruteForceIndex(
                self._db,
                "memory_vectors_fb" if self._table == "memory_vectors" else "utterance_vectors_fb",
            ).search(query, k, candidates)
        # unit-vector L2 distance → cosine similarity, clamped (metric-portable)
        return {r["id"]: max(-1.0, min(1.0, 1.0 - (r["distance"] ** 2) / 2.0)) for r in rows}


_VEC_ID_COL = {"memory_vectors": "memory_id", "utterance_vectors": "utterance_id"}


def make_memory_index(db: Database, dim: int) -> VectorIndex:
    if db.using_vec:
        try:
            return SqliteVecIndex(db, "memory_vectors", dim)
        except Exception as exc:  # noqa: BLE001 - creation failure degrades, never breaks
            log.warning("vec0 memory index unavailable (%s); brute-force fallback", exc)
    return BruteForceIndex(db, "memory_vectors_fb")


def make_utterance_index(db: Database, dim: int) -> VectorIndex:
    if db.using_vec:
        try:
            return SqliteVecIndex(db, "utterance_vectors", dim)
        except Exception as exc:  # noqa: BLE001
            log.warning("vec0 utterance index unavailable (%s); brute-force fallback", exc)
    return BruteForceIndex(db, "utterance_vectors_fb")
