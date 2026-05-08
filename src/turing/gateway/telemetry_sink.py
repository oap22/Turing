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
from turing.telemetry.gap_detector import GapDetected, GapDetector, Reset

logger = structlog.get_logger(__name__)

WSSendFn = Callable[[dict[str, Any]], Awaitable[None]]
Unsubscribe = Callable[[], None]


class TelemetrySink:
    def __init__(
        self, *, buffer: RingBuffer, reorder_window: int = 4
    ) -> None:
        self._buffer = buffer
        self._subscribers: list[WSSendFn] = []
        self._gaps = GapDetector(reorder_window=reorder_window)

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

        # Gap detection runs after the event is durably stored so a gap
        # that's later resolved still shows in the historical query.
        stream = message.payload.get("stream") or _stream_from_event_type(
            event["event_type"]
        )
        outcome = self._gaps.observe(
            node=event["node_name"], stream=str(stream), seq=int(event["seq"])
        )
        if isinstance(outcome, GapDetected):
            await self._handle_gap(outcome, ts_ms=int(event["timestamp_ms"]))
        elif isinstance(outcome, Reset):
            await self._handle_reset(outcome, ts_ms=int(event["timestamp_ms"]))

    async def _handle_gap(self, gap: GapDetected, *, ts_ms: int) -> None:
        marker_event = {
            "node_name": gap.node,
            "event_type": "gap_marker",
            "seq": 0,
            "timestamp_ms": ts_ms,
            "duration_ms": None,
            "error": None,
            "payload": {
                "stream": gap.stream,
                "missing": [gap.missing[0], gap.missing[1]],
            },
        }
        await self._buffer.append(marker_event)
        frame = {
            "type": "gap_marker",
            "node_name": gap.node,
            "stream": gap.stream,
            "missing": [gap.missing[0], gap.missing[1]],
            "timestamp_ms": ts_ms,
        }
        await self._broadcast(frame)

    async def _handle_reset(self, reset: Reset, *, ts_ms: int) -> None:
        frame = {
            "type": "stream_reset",
            "node_name": reset.node,
            "stream": reset.stream,
            "timestamp_ms": ts_ms,
        }
        await self._broadcast(frame)

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
        for frame in (trace_frame, metric_frame):
            await self._broadcast(frame)

    async def _broadcast(self, frame: dict[str, Any]) -> None:
        for send in list(self._subscribers):
            try:
                await send(frame)
            except Exception:
                logger.warning("telemetry_sink_send_failed", exc_info=True)


def _stream_from_event_type(event_type: str) -> str:
    """``llm.complete.end`` → ``llm.complete``."""
    return event_type.rsplit(".", 1)[0] if "." in event_type else event_type
