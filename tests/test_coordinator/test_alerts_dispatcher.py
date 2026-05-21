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


def _disk_peer(disk_used_bytes: int, disk_total_bytes: int) -> Any:
    specs = SimpleNamespace(
        disk_used_bytes=disk_used_bytes,
        disk_total_bytes=disk_total_bytes,
    )
    return SimpleNamespace(node_id="pi-gamma", name="pi-gamma", specs=specs)


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


@pytest.mark.asyncio
async def test_disk_danger_emits_one_alert_frame() -> None:
    """A peer past DISK_DANGER fires exactly one ``disk_pct`` frame."""
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    dispatcher = AlertDispatcher(AlertEngine(now_ms=lambda: 1_700_000_000_000), send_frame=sink)
    # 96 / 100 bytes → 96 % disk usage → danger. N_DANGER = 2.
    hot = _disk_peer(disk_used_bytes=96, disk_total_bytes=100)
    await dispatcher.observe(hot)
    assert frames == []
    await dispatcher.observe(hot)
    assert len(frames) == 1
    assert frames[0]["field"] == "disk_pct"
    assert frames[0]["severity"] == "danger"
    assert frames[0]["state"] == "alerting"
    assert frames[0]["value"] == 96.0
    assert frames[0]["threshold"] == 95.0


@pytest.mark.asyncio
async def test_zero_disk_total_never_alerts() -> None:
    """Test δ: ``disk_total_bytes == 0`` is the collector's sentinel for
    'collection failed on this platform' — it must never produce a disk
    alert, no matter how large ``disk_used_bytes`` is."""
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    dispatcher = AlertDispatcher(send_frame=sink)
    peer = _disk_peer(disk_used_bytes=10**12, disk_total_bytes=0)
    for _ in range(10):
        await dispatcher.observe(peer)
    assert frames == []
    assert dispatcher.engine.state_of("pi-gamma", "disk_pct").value == "clear"
