"""Tests for telemetry.bus + @traced decorator — PRD #38 US 16, 19, 20."""

from __future__ import annotations

import asyncio

import pytest

from turing.telemetry.bus import Telemetry, traced


@pytest.fixture
def bus():
    return Telemetry(node_id="test-node-1", node_name="test")


class TestEmit:
    def test_emit_increments_seq_per_stream(self, bus):
        bus.emit_sync("llm.call.start", payload={"k": 1})
        bus.emit_sync("llm.call.start", payload={"k": 2})
        bus.emit_sync("tool.dispatch.start", payload={})
        events = bus.drain_for_test()
        llm = [e for e in events if e.event_type == "llm.call.start"]
        tool = [e for e in events if e.event_type == "tool.dispatch.start"]
        assert [e.seq for e in llm] == [1, 2]
        assert [e.seq for e in tool] == [1]

    def test_event_carries_node_identity_and_priority(self, bus):
        bus.emit_sync("safety.gate.decision", payload={"d": "DENIED"}, priority="high")
        ev = bus.drain_for_test()[0]
        assert ev.node_id == "test-node-1"
        assert ev.node_name == "test"
        assert ev.priority == "high"
        assert ev.ts > 0

    def test_emit_is_non_blocking_when_queue_full(self, bus):
        # Tiny queue; emit_sync must not raise even if downstream stalls.
        bus = Telemetry(node_id="n", node_name="n", max_queue_size=2)
        for i in range(50):
            bus.emit_sync("e", payload={"i": i})
        # If we got here without blocking/raising, contract is satisfied.
        # Drained events ≤ queue size + drained-counter accounting.
        assert bus.dropped_count >= 48


class TestTracedSync:
    def test_traced_emits_start_and_end_with_duration(self, bus):
        @traced("op.work", bus=bus)
        def work(x):
            return x * 2

        assert work(3) == 6
        events = bus.drain_for_test()
        types = [e.event_type for e in events]
        assert types == ["op.work.start", "op.work.end"]
        assert events[1].payload["duration_ms"] >= 0

    def test_traced_emits_error_event_on_exception(self, bus):
        @traced("op.boom", bus=bus)
        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError):
            boom()
        events = bus.drain_for_test()
        types = [e.event_type for e in events]
        assert types == ["op.boom.start", "op.boom.error"]
        assert events[1].payload["error_type"] == "ValueError"


class TestTracedAsync:
    @pytest.mark.asyncio
    async def test_async_traced_emits_start_and_end(self, bus):
        @traced("op.async_work", bus=bus)
        async def work():
            await asyncio.sleep(0)
            return 42

        result = await work()
        assert result == 42
        events = bus.drain_for_test()
        assert [e.event_type for e in events] == ["op.async_work.start", "op.async_work.end"]

    @pytest.mark.asyncio
    async def test_async_traced_emits_error(self, bus):
        @traced("op.async_boom", bus=bus)
        async def boom():
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            await boom()
        events = bus.drain_for_test()
        assert events[-1].event_type == "op.async_boom.error"
