"""Mesh-transport sink for telemetry events.

The bus emits :class:`TelemetryEvent` objects in-process; this sink translates
each one into a :class:`MeshMessage` and shouts it on the configured Zyre group
(``"telemetry"`` by convention). A receiving node attaches a
:class:`TelemetrySubscriber` to its mesh client and gets an info-level log
line per received event — that's enough to prove the pipeline before the
gateway lands in slice 3.

TCP-WHISPER fallback for never-drop events is reserved for slice 8; for now
events tagged ``priority="high"`` ride the same SHOUT but as
``MessageType.TELEMETRY_PRIORITY`` so the receiver can already distinguish them.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Protocol

import structlog

from turing.mesh.protocol import MeshMessage, MessageType
from turing.telemetry.bus import TelemetryEvent

logger = structlog.get_logger(__name__)


class _Publisher(Protocol):
    def shout(self, group: str, message: MeshMessage) -> None: ...


class MeshTelemetrySink:
    """Callable telemetry sink that shouts events on a Zyre group."""

    def __init__(
        self,
        publisher: _Publisher,
        *,
        group: str,
        node_id: str,
        node_name: str,
    ) -> None:
        self._publisher = publisher
        self._group = group
        self._node_id = node_id
        self._node_name = node_name

    def __call__(self, event: TelemetryEvent) -> None:
        is_priority = event.payload.get("priority") == "high"
        msg_type = (
            MessageType.TELEMETRY_PRIORITY if is_priority else MessageType.TELEMETRY
        )
        msg = MeshMessage(
            type=msg_type,
            sender_id=self._node_id,
            sender_name=self._node_name,
            payload={
                "event_name": event.name,
                "stream": event.stream,
                "seq": event.seq,
                "timestamp_ms": event.timestamp_ms,
                "duration_ms": event.duration_ms,
                "error": event.error,
                "payload": dict(event.payload),
            },
        )
        try:
            self._publisher.shout(self._group, msg)
        except Exception:
            logger.warning("telemetry_mesh_publish_failed", exc_info=True)


class TelemetrySubscriber:
    """Receiver-side hook: log incoming telemetry events at info level.

    Pi-alpha installs this temporary subscriber to prove end-to-end flow
    before the SQLite ring buffer + gateway land in slice 4.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger("turing.telemetry.received")

    def on_message(self, message: MeshMessage) -> None:
        if message.type not in (MessageType.TELEMETRY, MessageType.TELEMETRY_PRIORITY):
            return
        self._handle(message)

    def _handle(self, message: MeshMessage) -> None:
        p = message.payload
        self._log.info(
            "telemetry_received",
            node_name=message.sender_name,
            event_type=p.get("event_name"),
            stream=p.get("stream"),
            seq=p.get("seq"),
            duration_ms=p.get("duration_ms"),
            priority=message.type is MessageType.TELEMETRY_PRIORITY,
        )
