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
    TEMP_DANGER,
    TEMP_WARN,
    severity_for_temp,
)

if TYPE_CHECKING:
    from turing.mesh.node import PeerInfo

logger = structlog.get_logger(__name__)

SendFrame = Callable[[dict], Awaitable[None]]


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
        """Feed one heartbeat sample into the engine, push any emitted event.

        Catches and logs exceptions from ``send_frame`` so a flaky WS
        subscriber cannot break the heartbeat handler. The engine itself
        is pure and is allowed to raise.
        """
        specs = getattr(peer, "specs", None)
        if specs is None:
            return
        temp = getattr(specs, "temp_celsius", None)
        severity = severity_for_temp(temp)
        _, event = self._engine.step(
            peer.node_id,
            "temp_celsius",
            severity,
            value=float(temp) if temp is not None else 0.0,
            threshold=TEMP_WARN,
            node_name=peer.name,
        )
        if event is None:
            return
        # Engine reports a single threshold; fix it up to match the alert's
        # *fired* severity so the banner can render the right one.
        if event.severity == "danger":
            event = replace(event, threshold=TEMP_DANGER)
        else:
            event = replace(event, threshold=TEMP_WARN)
        if self._send_frame is None:
            logger.debug("alert_drop_no_sink", node_id=peer.node_id, field="temp_celsius")
            return
        with contextlib.suppress(Exception):
            await self._send_frame(event.to_frame())
