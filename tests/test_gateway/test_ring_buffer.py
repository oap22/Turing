"""Tests for the SQLite-backed telemetry ring buffer (#42)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig

if TYPE_CHECKING:
    from pathlib import Path


def _evt(
    *,
    timestamp_ms: int,
    node_name: str = "pi-alpha",
    event_type: str = "tool.dispatch.end",
    seq: int = 1,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "node_name": node_name,
        "event_type": event_type,
        "seq": seq,
        "timestamp_ms": timestamp_ms,
        "duration_ms": 12.0,
        "payload": payload or {"k": "v"},
    }


@pytest.fixture
async def buffer(tmp_path: Path):
    cfg = RingBufferConfig(
        path=tmp_path / "telemetry.db",
        retention_seconds=24 * 3600,
        max_bytes=500 * 1024 * 1024,
    )
    buf = RingBuffer(cfg)
    await buf.open()
    yield buf
    await buf.close()


# ── append + query ─────────────────────────────────────────────────────


class TestAppendAndQuery:
    @pytest.mark.asyncio
    async def test_appended_event_is_queryable(self, buffer: RingBuffer) -> None:
        await buffer.append(_evt(timestamp_ms=1000))
        rows = await buffer.query()
        assert len(rows) == 1
        assert rows[0]["timestamp_ms"] == 1000

    @pytest.mark.asyncio
    async def test_query_filters_by_node(self, buffer: RingBuffer) -> None:
        await buffer.append(_evt(timestamp_ms=1, node_name="pi-alpha"))
        await buffer.append(_evt(timestamp_ms=2, node_name="pi-beta"))
        rows = await buffer.query(node_name="pi-beta")
        assert len(rows) == 1
        assert rows[0]["node_name"] == "pi-beta"

    @pytest.mark.asyncio
    async def test_query_filters_by_event_type(self, buffer: RingBuffer) -> None:
        await buffer.append(_evt(timestamp_ms=1, event_type="tool.dispatch.end"))
        await buffer.append(_evt(timestamp_ms=2, event_type="llm.complete.end"))
        rows = await buffer.query(event_type="llm.complete.end")
        assert [r["event_type"] for r in rows] == ["llm.complete.end"]

    @pytest.mark.asyncio
    async def test_query_filters_since_timestamp(self, buffer: RingBuffer) -> None:
        await buffer.append(_evt(timestamp_ms=100))
        await buffer.append(_evt(timestamp_ms=200))
        await buffer.append(_evt(timestamp_ms=300))
        rows = await buffer.query(since_ms=200)
        assert sorted(r["timestamp_ms"] for r in rows) == [200, 300]

    @pytest.mark.asyncio
    async def test_query_returns_payload_intact(self, buffer: RingBuffer) -> None:
        await buffer.append(_evt(timestamp_ms=1, payload={"provider": "cloud", "model": "claude"}))
        rows = await buffer.query()
        assert rows[0]["payload"] == {"provider": "cloud", "model": "claude"}


# ── TTL eviction ───────────────────────────────────────────────────────


class TestTtlPrune:
    @pytest.mark.asyncio
    async def test_prune_removes_events_older_than_retention(self, tmp_path: Path) -> None:
        cfg = RingBufferConfig(
            path=tmp_path / "t.db",
            retention_seconds=10,  # 10s
            max_bytes=10**9,
        )
        buf = RingBuffer(cfg)
        await buf.open()
        try:
            await buf.append(_evt(timestamp_ms=1000))  # fresh
            await buf.append(_evt(timestamp_ms=100_000_000))  # very old (boom)
            # Set "now" so the second event is past retention.
            removed = await buf.prune(now_ms=1000 + 11_000)
            assert removed >= 1
            rows = await buf.query()
            assert all(r["timestamp_ms"] >= 1000 for r in rows)
        finally:
            await buf.close()


# ── size cap FIFO eviction ────────────────────────────────────────────


class TestFifoEviction:
    @pytest.mark.asyncio
    async def test_oldest_rows_dropped_when_cap_exceeded(self, tmp_path: Path) -> None:
        # Tight cap: each row's payload is a few hundred bytes so a handful
        # easily blows the budget.
        cfg = RingBufferConfig(
            path=tmp_path / "t.db",
            retention_seconds=10**9,
            max_bytes=4096,  # 4 KB
        )
        buf = RingBuffer(cfg)
        await buf.open()
        try:
            big = "x" * 2048
            for i in range(10):
                await buf.append(
                    _evt(
                        timestamp_ms=1000 + i,
                        payload={"big": big, "i": i},
                    )
                )
            rows = await buf.query()
            timestamps = [r["timestamp_ms"] for r in rows]
            # The very first event must have been FIFO-evicted.
            assert 1000 not in timestamps
            # The most-recent event is still present.
            assert 1009 in timestamps
        finally:
            await buf.close()


# ── restart survival ──────────────────────────────────────────────────


class TestRestartSurvival:
    @pytest.mark.asyncio
    async def test_events_persist_across_reopen(self, tmp_path: Path) -> None:
        cfg = RingBufferConfig(
            path=tmp_path / "persist.db",
            retention_seconds=10**9,
            max_bytes=10**9,
        )
        buf = RingBuffer(cfg)
        await buf.open()
        await buf.append(_evt(timestamp_ms=42))
        await buf.close()

        # Re-open the same path
        buf2 = RingBuffer(cfg)
        await buf2.open()
        try:
            rows = await buf2.query()
            assert len(rows) == 1
            assert rows[0]["timestamp_ms"] == 42
        finally:
            await buf2.close()
