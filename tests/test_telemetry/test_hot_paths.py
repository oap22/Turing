"""Tests that @traced is applied to the agreed hot-path methods.

Acceptance criterion 4 of issue #40 says: tool dispatches, memory queries,
learning extraction, agent loop iterations, and safety-gate decisions are
all traced. Rather than couple to internals, we run real calls with a fresh
Telemetry singleton attached and assert the expected events appear.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from turing.agent.safety import SafetyGate
from turing.telemetry.bus import Telemetry, TelemetryEvent
from turing.tools.base import RiskLevel, Tool, ToolRegistry, ToolResult


class _FakeTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "echo args back"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, **kwargs) -> ToolResult:  # type: ignore[no-untyped-def]
        return ToolResult(success=True, output="hi")


@pytest.fixture
def tel(monkeypatch: pytest.MonkeyPatch) -> Telemetry:
    fresh = Telemetry()
    monkeypatch.setattr("turing.telemetry.bus._SINGLETON", fresh)
    return fresh


class TestToolDispatchTraced:
    @pytest.mark.asyncio
    async def test_tool_registry_execute_emits_events(self, tel: Telemetry) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        registry = ToolRegistry()
        registry.register(_FakeTool())
        await registry.execute("echo")

        names = [e.name for e in captured]
        assert "tool.dispatch.start" in names
        assert "tool.dispatch.end" in names

    @pytest.mark.asyncio
    async def test_tool_event_payload_includes_tool_name(
        self, tel: Telemetry
    ) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        registry = ToolRegistry()
        registry.register(_FakeTool())
        await registry.execute("echo")

        end = next(e for e in captured if e.name == "tool.dispatch.end")
        assert end.payload.get("tool") == "echo"


class TestSafetyGateTraced:
    @pytest.mark.asyncio
    async def test_safety_check_emits_priority_high_event(
        self, tel: Telemetry
    ) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        config = MagicMock(discord_admin_ids=[])
        gate = SafetyGate(config=config)
        await gate.check(
            tool_name="shell",
            arguments={"command": "rm -rf /"},
            user_id="user-1",
        )

        # Every safety event is tagged priority=high (start, end, error).
        for evt in captured:
            assert evt.name.startswith("safety.check")
            assert evt.payload.get("priority") == "high"

        end = next(e for e in captured if e.name == "safety.check.end")
        assert end.payload.get("decision") == "denied"
        assert "rm" in end.payload.get("rule", "").lower()


class TestMemoryRetrieverTraced:
    @pytest.mark.asyncio
    async def test_retrieve_emits_events(self, tel: Telemetry) -> None:
        # MemoryRetriever pulls in embeddings → numpy. Skip if the local
        # numpy install is broken; CI's clean environment will run this.
        pytest.importorskip("numpy")

        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        from turing.memory.retriever import MemoryRetriever

        store = MagicMock()
        store.get_messages = AsyncMock(return_value=[])
        store.get_user_preferences = AsyncMock(return_value={})
        store.get_facts = AsyncMock(return_value=[])
        vectors = MagicMock()
        vectors.search = AsyncMock(return_value=[])
        embeddings = MagicMock()
        embeddings.embed = MagicMock(return_value=[0.0] * 8)

        retriever = MemoryRetriever(store, vectors, embeddings)
        await retriever.retrieve("hi", channel_id="c", user_id="u")

        names = [e.name for e in captured]
        assert "memory.retrieve.start" in names
        assert "memory.retrieve.end" in names


class TestPerformanceBudget:
    """Telemetry overhead must not stall the agent loop."""

    @pytest.mark.asyncio
    async def test_traced_tool_dispatch_overhead_is_bounded(
        self, tel: Telemetry
    ) -> None:
        # 100 dispatches under 1 second is generous — the actual budget is
        # usually well under 50ms in real runs; we leave a safety margin so
        # this test is not flaky on slow CI workers.
        import time

        registry = ToolRegistry()
        registry.register(_FakeTool())
        start = time.monotonic()
        for _ in range(100):
            await registry.execute("echo")
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"100 traced tool dispatches took {elapsed:.3f}s"
