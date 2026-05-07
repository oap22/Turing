"""TelemetrySink — drain mesh-borne telemetry into the ring buffer + fan out to WS.

Replaces the slice-2 ``TelemetrySubscriber`` log-on-receive stub. Each
inbound ``TELEMETRY`` (or ``TELEMETRY_PRIORITY``) message is appended to the
ring buffer and then fanned out as two WebSocket frames:

- ``message_trace``: the redacted event for the trace pane (slice 7)
- ``metric``: the duration / error fields for the live call-graph (slice 6)

A subscriber callable (typically a per-WS-client send function) registers via
``subscribe()``. Failures in a subscriber are logged and swallowed so a
flaky browser cannot break the rest of the fan-out.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from turing.gateway.ring_buffer import RingBuffer
from turing.mesh.protocol import MeshMessage, MessageType

logger = structlog.get_logger(__name__)

WSSendFn = Callable[[dict[str, Any]], Awaitable[None]]
Unsubscribe = Callable[[], None]


class TelemetrySink:
    def __init__(self, *, buffer: RingBuffer) -> None:
        self._buffer = buffer
        self._subscribers: list[WSSendFn] = []

    def subscribe(self, send: WSSendFn) -> Unsubscribe:
        self._subscribers.append(send)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(send)
            except ValueError:
                pass

        return _unsubscribe

    async def on_mesh_message(self, message: MeshMessage) -> None:
        if message.type not in (
            MessageType.TELEMETRY,
            MessageType.TELEMETRY_PRIORITY,
        ):
            return
        event = self._unpack(message)
        await self._buffer.append(event)
        await self._fanout(event, priority=message.type is MessageType.TELEMETRY_PRIORITY)

    @staticmethod
    def _unpack(message: MeshMessage) -> dict[str, Any]:
        p = message.payload
        return {
            "node_name": message.sender_name,
            "event_type": p.get("event_name", ""),
            "seq": p.get("seq", 0),
            "timestamp_ms": p.get("timestamp_ms", 0),
            "duration_ms": p.get("duration_ms"),
            "error": p.get("error"),
            "payload": p.get("payload") or {},
        }

    async def _fanout(self, event: dict[str, Any], *, priority: bool) -> None:
        trace_frame = {
            "type": "message_trace",
            "node_name": event["node_name"],
            "event_type": event["event_type"],
            "seq": event["seq"],
            "timestamp_ms": event["timestamp_ms"],
            "duration_ms": event["duration_ms"],
            "error": event["error"],
            "payload": event["payload"],
            "priority": priority,
        }
        metric_frame = {
            "type": "metric",
            "node_name": event["node_name"],
            "event_type": event["event_type"],
            "duration_ms": event["duration_ms"],
            "error": event["error"],
            "timestamp_ms": event["timestamp_ms"],
        }
        for send in list(self._subscribers):
            for frame in (trace_frame, metric_frame):
                try:
                    await send(frame)
                except Exception:
                    logger.warning("telemetry_sink_send_failed", exc_info=True)
