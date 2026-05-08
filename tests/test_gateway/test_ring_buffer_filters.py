"""Tests for the new ring-buffer filter parameters (#45)."""

from __future__ import annotations

from pathlib import Path

import pytest

from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig


@pytest.fixture
async def buffer(tmp_path: Path):
    cfg = RingBufferConfig(
        path=tmp_path / "t.db",
        retention_seconds=10**9,
        max_bytes=10**9,
    )
    buf = RingBuffer(cfg)
    await buf.open()
    await buf.append({
        "timestamp_ms": 1000, "node_name": "a", "event_type": "x.start",
        "seq": 1, "duration_ms": 5, "payload": {},
    })
    await buf.append({
        "timestamp_ms": 2000, "node_name": "a", "event_type": "x.end",
        "seq": 2, "duration_ms": 50, "payload": {},
    })
    await buf.append({
        "timestamp_ms": 3000, "node_name": "b", "event_type": "y.end",
        "seq": 1, "duration_ms": 200, "payload": {},
    })
    await buf.append({
        "timestamp_ms": 4000, "node_name": "b", "event_type": "z.end",
        "seq": 1, "duration_ms": 800, "payload": {},
    })
    yield buf
    await buf.close()


class TestNodeNamesList:
    @pytest.mark.asyncio
    async def test_query_filters_by_multiple_nodes(self, buffer: RingBuffer) -> None:
        rows = await buffer.query(node_names=("a",))
        assert {r["node_name"] for r in rows} == {"a"}

    @pytest.mark.asyncio
    async def test_query_filters_by_set_of_nodes(self, buffer: RingBuffer) -> None:
        rows = await buffer.query(node_names=("a", "b"))
        assert {r["node_name"] for r in rows} == {"a", "b"}


class TestEventTypesList:
    @pytest.mark.asyncio
    async def test_query_filters_by_multiple_event_types(
        self, buffer: RingBuffer
    ) -> None:
        rows = await buffer.query(event_types=("x.end", "y.end"))
        assert {r["event_type"] for r in rows} == {"x.end", "y.end"}


class TestMinDuration:
    @pytest.mark.asyncio
    async def test_filters_by_min_duration_ms(self, buffer: RingBuffer) -> None:
        rows = await buffer.query(min_duration_ms=100)
        assert all(r["duration_ms"] >= 100 for r in rows)
        assert len(rows) == 2


class TestLimitOffset:
    @pytest.mark.asyncio
    async def test_limit_caps_results(self, buffer: RingBuffer) -> None:
        rows = await buffer.query(limit=2)
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_offset_skips_earlier_rows(self, buffer: RingBuffer) -> None:
        first = await buffer.query(limit=2)
        second = await buffer.query(limit=2, offset=2)
        assert first[0]["timestamp_ms"] != second[0]["timestamp_ms"]


class TestBackwardsCompat:
    """Existing `node_name` / `event_type` (singular) callers still work."""

    @pytest.mark.asyncio
    async def test_singular_node_name_still_supported(
        self, buffer: RingBuffer
    ) -> None:
        rows = await buffer.query(node_name="a")
        assert {r["node_name"] for r in rows} == {"a"}

    @pytest.mark.asyncio
    async def test_singular_event_type_still_supported(
        self, buffer: RingBuffer
    ) -> None:
        rows = await buffer.query(event_type="x.end")
        assert {r["event_type"] for r in rows} == {"x.end"}
