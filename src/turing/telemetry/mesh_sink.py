"""Mesh-transport sink for telemetry events.

The bus emits :class:`TelemetryEvent` objects in-process; this sink translates
each one into a :class:`MeshMessage` and ships it across the mesh.

Two routes:

- **SHOUT** — multicast UDP to every member of the configured Zyre group
  (``"telemetry"`` by convention). Lossy under load but cheap; this is the
  baseline path for ordinary telemetry.
- **WHISPER** — direct TCP unicast to ``priority_peer`` (typically pi-alpha).
  Used for events tagged ``priority="high"`` (errors, safety-gate denials)
  so the most diagnostically-valuable events survive UDP loss.

If ``priority_peer`` is ``None`` the sink falls back to SHOUT for priority
events too — that's no worse than the slice-2 baseline and lets dev nodes
run without a configured gateway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import structlog

from turing.mesh.protocol import MeshMessage, MessageType

if TYPE_CHECKING:
    from turing.telemetry.bus import TelemetryEvent

logger = structlog.get_logger(__name__)


class _Publisher(Protocol):
    def shout(self, group: str, message: MeshMessage) -> None: ...

    # whisper is optional — not all publishers will implement it (the slice-2
    # capturing fake doesn't, for example), so callers must check before use.


class MeshTelemetrySink:
    """Callable telemetry sink that shouts events on a Zyre group.

    Priority events (``payload['priority'] == 'high'``) route via WHISPER to
    ``priority_peer`` when one is configured; otherwise they fall back to a
    priority-tagged SHOUT.
    """

    def __init__(
        self,
        publisher: _Publisher,
        *,
        group: str,
        node_id: str,
        node_name: str,
        priority_peer: str | None = None,
    ) -> None:
        self._publisher = publisher
        self._group = group
        self._node_id = node_id
        self._node_name = node_name
        self._priority_peer = priority_peer

    def __call__(self, event: TelemetryEvent) -> None:
        is_priority = event.payload.get("priority") == "high"
        msg_type = MessageType.TELEMETRY_PRIORITY if is_priority else MessageType.TELEMETRY
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
            if is_priority and self._priority_peer and hasattr(self._publisher, "whisper"):
                self._publisher.whisper(self._priority_peer, msg)  # type: ignore[attr-defined]
            else:
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
