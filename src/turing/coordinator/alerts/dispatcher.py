"""AlertDispatcher — wires the AlertEngine to the gateway's WS fan-out.

The dispatcher is the single place that translates a heartbeat into an
``alert`` frame on the wire. It samples the relevant fields off ``PeerInfo``,
runs them through the engine, and on any emitted event calls ``send_frame``
(typically the gateway's telemetry-sink broadcast). Discord, snooze, and
reachability live in later slices and intentionally do not appear here.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING

import structlog

from turing.coordinator.alerts.engine import AlertEngine
from turing.coordinator.alerts.types import (
    DISK_DANGER,
    DISK_WARN,
    TEMP_DANGER,
    TEMP_WARN,
    Field,
    Severity,
    disk_percent,
    severity_for_disk,
    severity_for_temp,
)

if TYPE_CHECKING:
    from turing.mesh.node import PeerInfo

logger = structlog.get_logger(__name__)

SendFrame = Callable[[dict], Awaitable[None]]

# warn / danger threshold pair per alertable field. The engine is fed the
# warn threshold; ``observe`` swaps in the danger threshold when the fired
# event's severity is danger, so the banner renders the right number.
_THRESHOLDS: dict[Field, tuple[float, float]] = {
    "temp_celsius": (TEMP_WARN, TEMP_DANGER),
    "disk_pct": (DISK_WARN, DISK_DANGER),
}


class AlertDispatcher:
    def __init__(
        self,
        engine: AlertEngine | None = None,
        *,
        send_frame: SendFrame | None = None,
    ) -> None:
        self._engine = engine or AlertEngine()
        self._send_frame: SendFrame | None = send_frame

    @property
    def engine(self) -> AlertEngine:
        return self._engine

    def set_send_frame(self, send_frame: SendFrame | None) -> None:
        """Wire (or unwire) the fan-out target.

        Late binding lets the gateway construct the dispatcher before the
        telemetry sink exists, then attach the sink during ``create_app``.
        """
        self._send_frame = send_frame

    async def observe(self, peer: PeerInfo, now_ms: int | None = None) -> None:
        """Feed one heartbeat sample into the engine, push any emitted events.

        Evaluates every alertable field (TEMP + DISK) for this peer. Each
        field has its own ``(peer, field)`` state machine; a peer can be
        alerting on one field and clear on the other independently.

        Catches and logs exceptions from ``send_frame`` so a flaky WS
        subscriber cannot break the heartbeat handler. The engine itself
        is pure and is allowed to raise.
        """
        specs = getattr(peer, "specs", None)
        if specs is None:
            return
        for field, severity, value in self._sample(specs):
            warn_threshold, danger_threshold = _THRESHOLDS[field]
            _, event = self._engine.step(
                peer.node_id,
                field,
                severity,
                value=value,
                threshold=warn_threshold,
                node_name=peer.name,
            )
            if event is None:
                continue
            # Engine reports a single threshold; fix it up to match the
            # alert's *fired* severity so the banner renders the right one.
            threshold = danger_threshold if event.severity == "danger" else warn_threshold
            event = replace(event, threshold=threshold)
            if self._send_frame is None:
                logger.debug("alert_drop_no_sink", node_id=peer.node_id, field=field)
                continue
            with contextlib.suppress(Exception):
                await self._send_frame(event.to_frame())

    @staticmethod
    def _sample(specs: object) -> list[tuple[Field, Severity, float]]:
        """Read each alertable field off a peer's specs.

        Returns ``(field, severity, value)`` triples. ``value`` is the raw
        reading the banner displays — degrees for TEMP, a percentage for
        DISK. A missing field reads as ``ok`` so older peers don't alert.
        """
        temp = getattr(specs, "temp_celsius", None)
        dpct = disk_percent(
            getattr(specs, "disk_used_bytes", None),
            getattr(specs, "disk_total_bytes", None),
        )
        return [
            (
                "temp_celsius",
                severity_for_temp(temp),
                float(temp) if temp is not None else 0.0,
            ),
            (
                "disk_pct",
                severity_for_disk(dpct),
                dpct if dpct is not None else 0.0,
            ),
        ]
