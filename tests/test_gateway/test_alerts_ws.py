"""Test δ: an ``alert`` frame produced by the dispatcher reaches a real WS subscriber.

Uses FastAPI's ``TestClient.websocket_connect`` so the frame traverses the
actual WS endpoint and the telemetry-sink fan-out — closing the
"every β/γ test stubs the sink" gap.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from turing.coordinator.alerts.dispatcher import AlertDispatcher
from turing.coordinator.alerts.engine import AlertEngine
from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth
from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig
from turing.gateway.telemetry_sink import TelemetrySink


@pytest.mark.asyncio
async def test_alert_frame_reaches_connected_ws_subscriber(tmp_path) -> None:  # type: ignore[no-untyped-def]
    auth = GatewayAuth(token="t0k3n")  # type: ignore[call-arg]
    cfg = RingBufferConfig(path=tmp_path / "t.db", retention_seconds=10**9, max_bytes=10**9)
    buffer = RingBuffer(cfg)
    await buffer.open()
    sink = TelemetrySink(buffer=buffer)
    dispatcher = AlertDispatcher(AlertEngine(now_ms=lambda: 1_700_000_000_000))

    app = create_app(
        auth=auth,
        node_name="pi-alpha",
        ring_buffer=buffer,
        telemetry_sink=sink,
        alert_dispatcher=dispatcher,
    )

    client = TestClient(app)
    headers = {"Authorization": "Bearer t0k3n"}
    with client.websocket_connect("/ws", headers=headers) as ws:
        # Drain the hello frame.
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"

        # Drive the dispatcher into ``alerting`` via three hot heartbeats
        # of the same fake peer.
        peer = SimpleNamespace(
            node_id="pi-beta",
            name="pi-beta",
            specs=SimpleNamespace(temp_celsius=83.0),
        )
        for _ in range(2):
            await dispatcher.observe(peer)
        # Second danger heartbeat trips the engine (N_DANGER=2); receive
        # the alert frame on the WS.
        msg = json.loads(ws.receive_text())

    await buffer.close()

    assert msg["type"] == "alert"
    assert msg["node_id"] == "pi-beta"
    assert msg["node_name"] == "pi-beta"
    assert msg["field"] == "temp_celsius"
    assert msg["severity"] == "danger"
    assert msg["state"] == "alerting"
    assert msg["value"] == 83.0
    assert msg["threshold"] == 82.0  # TEMP_DANGER for a danger-severity event
    assert msg["fired_at_ms"] == 1_700_000_000_000
