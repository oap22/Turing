"""Test γ: end-to-end ntfy fallback when the SPA is unreachable.

Wires a real ``TelemetrySink`` through ``create_app`` together with an
``AlertDispatcher`` whose reachability clock reads that sink. No WebSocket
client ever connects, so ``last_send_ms`` stays ``None`` for the whole test
— the bootstrap-as-unreachable case — and three hot heartbeats must escalate
to exactly one operator ntfy push (ADR-0010 §2; replaces the retired
Discord-DM fallback).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from turing.coordinator.alerts.dispatcher import AlertDispatcher
from turing.coordinator.alerts.engine import AlertEngine
from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth
from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig
from turing.gateway.telemetry_sink import TelemetrySink


class _FakeNtfy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ntfy_push(self, content: str) -> None:
        self.calls.append(content)


@pytest.mark.asyncio
async def test_hot_heartbeats_escalate_to_ntfy_when_spa_unreachable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = RingBufferConfig(path=tmp_path / "t.db", retention_seconds=10**9, max_bytes=10**9)
    buffer = RingBuffer(cfg)
    await buffer.open()
    sink = TelemetrySink(buffer=buffer)
    ntfy = _FakeNtfy()
    dispatcher = AlertDispatcher(
        AlertEngine(now_ms=lambda: 1_700_000_000_000),
        ntfy_client=ntfy,
        reachability_clock=lambda: sink.last_send_ms,
    )

    # create_app wires the dispatcher's fan-out through the telemetry sink.
    app = create_app(
        auth=GatewayAuth(token="t0k3n"),  # type: ignore[call-arg]
        node_name="pi-alpha",
        ring_buffer=buffer,
        telemetry_sink=sink,
        alert_dispatcher=dispatcher,
    )
    assert app.state.alert_dispatcher is dispatcher

    # Three hot heartbeats of the same peer → one alerting edge.
    peer = SimpleNamespace(
        node_id="pi-beta",
        name="pi-beta",
        specs=SimpleNamespace(temp_celsius=86.0),
    )
    for _ in range(3):
        await dispatcher.observe(peer)

    await buffer.close()

    # No WS client ever connected, so the SPA never received a frame.
    assert sink.last_send_ms is None
    # Exactly one push, on the alerting edge, with the one-line summary.
    assert ntfy.calls == ["⚠ pi-beta TEMP danger: 86.0°C (>82.0°C)"]
