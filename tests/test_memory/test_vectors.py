"""Tests for :class:`turing.memory.vectors.VectorStore`.

Drives the real sqlite-vec virtual table over an in-memory aiosqlite
connection: add / search / count / delete round-trips, the not-initialised
guard, and the extension-load-failure branch (semantic search degrades to
unavailable rather than crashing).
"""

from __future__ import annotations

import aiosqlite
import pytest

from turing.memory.vectors import VectorStore, _serialize_f32

_DIM = 4


async def _open_store(dimension: int = _DIM) -> tuple[VectorStore, aiosqlite.Connection]:
    db = await aiosqlite.connect(":memory:")
    store = VectorStore(dimension=dimension)
    await store.initialize(db)
    return store, db


def test_serialize_f32_round_trips_byte_length() -> None:
    blob = _serialize_f32([1.0, 2.0, 3.0])
    assert isinstance(blob, bytes)
    assert len(blob) == 3 * 4  # four bytes per float32


@pytest.mark.asyncio
async def test_db_property_raises_before_initialize() -> None:
    store = VectorStore(dimension=_DIM)
    with pytest.raises(RuntimeError, match="not initialized"):
        _ = store.db


@pytest.mark.asyncio
async def test_add_then_search_returns_nearest() -> None:
    store, db = await _open_store()
    try:
        await store.add(1, [1.0, 0.0, 0.0, 0.0])
        await store.add(2, [0.0, 1.0, 0.0, 0.0])
        await store.add(3, [0.9, 0.1, 0.0, 0.0])

        results = await store.search([1.0, 0.0, 0.0, 0.0], limit=2)

        assert len(results) == 2
        # Closest match (id 1, exact) comes first, ordered by ascending distance.
        ids = [memory_id for memory_id, _distance in results]
        assert ids[0] == 1
        assert all(isinstance(dist, float) for _id, dist in results)
        assert results[0][1] <= results[1][1]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_count_tracks_inserts_and_deletes() -> None:
    store, db = await _open_store()
    try:
        assert await store.count() == 0
        await store.add(1, [1.0, 0.0, 0.0, 0.0])
        await store.add(2, [0.0, 1.0, 0.0, 0.0])
        assert await store.count() == 2

        await store.delete(1)
        assert await store.count() == 1
        remaining = await store.search([0.0, 1.0, 0.0, 0.0], limit=5)
        assert [mid for mid, _ in remaining] == [2]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_initialize_degrades_when_extension_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await aiosqlite.connect(":memory:")
    try:
        # Simulate a host where the sqlite-vec extension can't be loaded:
        # initialize must warn + return, not raise.
        async def _boom(_enabled: bool) -> None:
            raise RuntimeError("extension loading disabled")

        monkeypatch.setattr(db, "enable_load_extension", _boom)

        store = VectorStore(dimension=_DIM)
        await store.initialize(db)  # must not raise

        # The connection is still wired up even though the vec table is absent.
        assert store.db is db
    finally:
        await db.close()
