"""Test β: AlertDispatcher fan-out.

Fakes the telemetry sink with a list-appender and the peer with a tiny
SimpleNamespace. Asserts exactly one ``alerting`` frame on the danger edge,
zero frames during steady alerting, and exactly one ``cleared`` frame on
return-to-ok.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from turing.coordinator.alerts.dispatcher import AlertDispatcher
from turing.coordinator.alerts.engine import AlertEngine


def _peer(temp_celsius: float | None) -> Any:
    specs = SimpleNamespace(temp_celsius=temp_celsius)
    return SimpleNamespace(node_id="pi-beta", name="pi-beta", specs=specs)


@pytest.mark.asyncio
async def test_alerting_and_cleared_each_emit_exactly_one_frame() -> None:
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    eng = AlertEngine(now_ms=lambda: 1_700_000_000_000)
    dispatcher = AlertDispatcher(eng, send_frame=sink)

    # 1 hot heartbeat → no frame yet (warn × 1).
    await dispatcher.observe(_peer(76.0))
    assert frames == []

    # 2 more → exactly one alerting frame.
    await dispatcher.observe(_peer(77.0))
    await dispatcher.observe(_peer(78.0))
    assert len(frames) == 1
    assert frames[0]["type"] == "alert"
    assert frames[0]["state"] == "alerting"
    assert frames[0]["severity"] == "warn"
    assert frames[0]["field"] == "temp_celsius"
    assert frames[0]["node_id"] == "pi-beta"

    # Continued hot heartbeats during the alerting run: no further frames.
    for _ in range(5):
        await dispatcher.observe(_peer(78.0))
    assert len(frames) == 1

    # Three cool heartbeats: one cleared frame.
    await dispatcher.observe(_peer(50.0))
    assert len(frames) == 1
    await dispatcher.observe(_peer(50.0))
    assert len(frames) == 1
    await dispatcher.observe(_peer(50.0))
    assert len(frames) == 2
    assert frames[1]["state"] == "cleared"


@pytest.mark.asyncio
async def test_no_sink_no_explosion() -> None:
    """Dispatcher with no ``send_frame`` doesn't raise; engine still advances."""
    dispatcher = AlertDispatcher(AlertEngine(now_ms=lambda: 0))
    for _ in range(3):
        await dispatcher.observe(_peer(80.0))
    # Engine state should still be ``alerting``.
    assert dispatcher.engine.state_of("pi-beta", "temp_celsius").value == "alerting"


@pytest.mark.asyncio
async def test_observe_skips_when_specs_missing() -> None:
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    dispatcher = AlertDispatcher(send_frame=sink)
    peer = SimpleNamespace(node_id="x", name="x", specs=None)
    await dispatcher.observe(peer)
    assert frames == []


@pytest.mark.asyncio
async def test_observe_swallows_sink_errors() -> None:
    """A flaky WS subscriber must not break the heartbeat handler."""

    async def sink(_frame: dict) -> None:
        raise RuntimeError("ws closed")

    dispatcher = AlertDispatcher(send_frame=sink)
    # Drive into alerting — this is where the sink call happens.
    for _ in range(3):
        await dispatcher.observe(_peer(80.0))
    # Surviving the loop is the assertion.
