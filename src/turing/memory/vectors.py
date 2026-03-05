"""Vector store backed by sqlite-vec for semantic similarity search."""

from __future__ import annotations

import struct
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

_DIMENSION = 384


def _serialize_f32(vec: list[float]) -> bytes:
    """Serialize a float vector to a compact binary format for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


class VectorStore:
    """Manages a sqlite-vec virtual table for embedding-based search.

    The store piggy-backs on the same aiosqlite connection used by
    :class:`MemoryStore`, so both live in a single database file.
    """

    def __init__(self, dimension: int = _DIMENSION) -> None:
        self._dimension = dimension
        self._db: aiosqlite.Connection | None = None

    # ── lifecycle ──────────────────────────────────────────────────────

    async def initialize(self, db: aiosqlite.Connection) -> None:
        """Create the virtual table if it does not exist.

        Parameters
        ----------
        db:
            An *already-opened* aiosqlite connection (typically the one
            owned by ``MemoryStore``).  The sqlite-vec extension must be
            loadable at runtime.
        """
        self._db = db

        try:
            await db.enable_load_extension(True)
            # sqlite-vec registers itself via the auto-load entry point
            # packaged by the ``sqlite-vec`` Python wheel.
            import sqlite_vec  # type: ignore[import-untyped]

            await db.load_extension(sqlite_vec.loadable_path())
            await db.enable_load_extension(False)
        except Exception:
            logger.warning(
                "sqlite-vec extension could not be loaded. "
                "Semantic search will be unavailable.",
                exc_info=True,
            )
            return

        await db.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory
            USING vec0(
                embedding float[{self._dimension}],
                memory_id integer
            )
            """
        )
        await db.commit()
        logger.info("vector_store_initialized", dimension=self._dimension)

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("VectorStore not initialized — call initialize(db) first")
        return self._db

    # ── CRUD ──────────────────────────────────────────────────────────

    async def add(self, memory_id: int, embedding: list[float]) -> None:
        """Insert an embedding for the given memory id."""
        blob = _serialize_f32(embedding)
        await self.db.execute(
            "INSERT INTO vec_memory (embedding, memory_id) VALUES (?, ?)",
            (blob, memory_id),
        )
        await self.db.commit()

    async def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[tuple[int, float]]:
        """Find the nearest neighbours to *query_embedding*.

        Returns a list of ``(memory_id, distance)`` pairs ordered by
        ascending distance (i.e. most similar first).
        """
        blob = _serialize_f32(query_embedding)
        cursor = await self.db.execute(
            """
            SELECT memory_id, distance
            FROM vec_memory
            WHERE embedding MATCH ?
            ORDER BY distance
            LIMIT ?
            """,
            (blob, limit),
        )
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]

    async def delete(self, memory_id: int) -> None:
        """Remove the embedding associated with *memory_id*."""
        await self.db.execute(
            "DELETE FROM vec_memory WHERE memory_id = ?",
            (memory_id,),
        )
        await self.db.commit()

    async def count(self) -> int:
        """Return the number of embeddings stored."""
        cursor = await self.db.execute("SELECT COUNT(*) FROM vec_memory")
        row = await cursor.fetchone()
        return row[0] if row else 0
