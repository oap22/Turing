"""AlertDispatcher — wires the AlertEngine to the gateway's WS fan-out.

The dispatcher is the single place that translates a heartbeat into an
``alert`` frame on the wire. It samples the relevant fields off ``PeerInfo``,
runs them through the engine, and on any emitted event calls ``send_frame``
(typically the gateway's telemetry-sink broadcast).

It also owns the per-``(peer, field)`` snooze map: while a key is snoozed
the engine keeps grading silently but no frames go out for it. The snooze
map is plain in-memory state — a coordinator restart clears every snooze,
by design (PRD #228). Discord fallback lands in slice 4.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

import structlog

from turing.coordinator.alerts.engine import AlertEngine
from turing.coordinator.alerts.types import (
    DISK_DANGER,
    DISK_WARN,
    TEMP_DANGER,
    TEMP_WARN,
    Alert,
    AlertState,
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
ReachabilityClock = Callable[[], "int | None"]

# Hardcoded snooze window — PRD #228 keeps this fixed until an operator
# asks for a configurable one.
DEFAULT_SNOOZE_MS = 4 * 60 * 60 * 1000

# SPA-reachability window. If the gateway has not pushed a frame within this
# many ms, the SPA is treated as unreachable and the alerting edge also
# escapes to Discord. A ``None`` last-send (no SPA has ever connected) is
# unreachable too — see PRD #228's bootstrap-as-unreachable decision.
REACHABLE_WINDOW_MS = 90_000


class DiscordClient(Protocol):
    """Structural type for the Discord DM fallback — see ``discord_client.py``."""

    async def dm_operator(self, content: str) -> None: ...


# warn / danger threshold pair per alertable field. The engine is fed the
# warn threshold; ``observe`` swaps in the danger threshold when the fired
# event's severity is danger, so the banner renders the right number.
_THRESHOLDS: dict[Field, tuple[float, float]] = {
    "temp_celsius": (TEMP_WARN, TEMP_DANGER),
    "disk_pct": (DISK_WARN, DISK_DANGER),
}

# Field labels for the one-line Discord summary.
_FIELD_LABEL: dict[Field, str] = {"temp_celsius": "TEMP", "disk_pct": "DISK"}


class AlertDispatcher:
    def __init__(
        self,
        engine: AlertEngine | None = None,
        *,
        send_frame: SendFrame | None = None,
        discord_client: DiscordClient | None = None,
        reachability_clock: ReachabilityClock | None = None,
    ) -> None:
        self._engine = engine or AlertEngine()
        self._send_frame: SendFrame | None = send_frame
        # (node_id, field) -> epoch-ms expiry. In-memory only, by design.
        self._snoozes: dict[tuple[str, Field], int] = {}
        # Most recent ``alerting`` event per key, so ``snooze`` can replay
        # it as a dimming ``update`` frame without re-deriving the reading.
        self._last_alert: dict[tuple[str, Field], Alert] = {}
        # Discord DM fallback for the closed-laptop case. ``None`` disables it.
        self._discord_client = discord_client
        # Returns the gateway's last successful WS push (epoch-ms), or None.
        self._reachability_clock = reachability_clock

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
        now = now_ms if now_ms is not None else int(time.time() * 1000)
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
            key = (peer.node_id, field)
            # Remember the latest alerting edge even when it's suppressed —
            # ``snooze`` replays it as the dimming ``update`` frame.
            if event.state == "alerting":
                self._last_alert[key] = event
            # A live snooze swallows both ``alerting`` and ``cleared`` edges
            # for this key — and the Discord fallback below with them; the
            # engine state still advanced above.
            if self._is_snoozed(key, now):
                logger.debug("alert_suppressed_snoozed", node_id=peer.node_id, field=field)
                continue
            await self._emit(event)
            # Only the alerting edge can escape to Discord — never cleared.
            if event.state == "alerting":
                await self._maybe_dm(event, now)

    async def snooze(
        self,
        node_id: str,
        field: Field,
        now_ms: int,
        duration_ms: int = DEFAULT_SNOOZE_MS,
    ) -> int:
        """Snooze a ``(peer, field)`` for ``duration_ms`` and return the expiry.

        While snoozed, ``observe`` keeps grading but emits no frames for the
        key. If the key is *already* ``alerting``, one ``update`` frame goes
        out immediately (state ``alerting``, non-null ``snoozed_until_ms``)
        so the SPA can dim the row without waiting for the next heartbeat.
        """
        expiry = now_ms + duration_ms
        self._snoozes[(node_id, field)] = expiry
        if self._engine.state_of(node_id, field) == AlertState.alerting:
            cached = self._last_alert.get((node_id, field))
            if cached is not None:
                await self._emit(replace(cached, snoozed_until_ms=expiry))
        return expiry

    def snoozed_until(self, node_id: str, field: Field) -> int | None:
        """Raw snooze expiry for a key, or ``None`` if it is not snoozed.

        Returns the stored value verbatim — callers compare against their
        own clock. A fresh dispatcher has an empty map (in-memory by design).
        """
        return self._snoozes.get((node_id, field))

    def _is_snoozed(self, key: tuple[str, Field], now_ms: int) -> bool:
        expiry = self._snoozes.get(key)
        if expiry is None:
            return False
        if expiry <= now_ms:
            del self._snoozes[key]  # expired — drop it so the key resumes
            return False
        return True

    async def _emit(self, event: Alert) -> None:
        if self._send_frame is None:
            logger.debug("alert_drop_no_sink", node_id=event.node_id, field=event.field)
            return
        with contextlib.suppress(Exception):
            await self._send_frame(event.to_frame())

    async def _maybe_dm(self, event: Alert, now_ms: int) -> None:
        """Escalate an ``alerting`` edge to Discord when the SPA is unreachable.

        The SPA counts as unreachable when there is no ``reachability_clock``,
        when the clock returns ``None`` (no SPA has ever connected — the
        bootstrap case), or when its last successful push is older than
        ``REACHABLE_WINDOW_MS``. A ``dm_operator`` that raises is caught here
        so a Discord failure never propagates out of ``observe``.
        """
        if self._discord_client is None:
            return
        if self._reachability_clock is not None:
            last_send_ms = self._reachability_clock()
            if last_send_ms is not None and now_ms - last_send_ms <= REACHABLE_WINDOW_MS:
                return  # SPA pushed recently — it will surface the banner itself
        try:
            await self._discord_client.dm_operator(_summarize(event))
        except Exception:
            logger.warning(
                "alert_discord_dm_failed",
                node_id=event.node_id,
                field=event.field,
                exc_info=True,
            )

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


def _summarize(event: Alert) -> str:
    """One-line operator summary for a Discord DM.

    e.g. ``⚠ pi-beta TEMP danger: 87.4°C (>82.0°C)``.
    """
    label = _FIELD_LABEL.get(event.field, event.field.upper())
    if event.field == "temp_celsius":
        reading = f"{event.value:.1f}°C (>{event.threshold:.1f}°C)"
    elif event.field == "disk_pct":
        reading = f"{round(event.value)}% (>{round(event.threshold)}%)"
    else:  # pragma: no cover — defensive; only two fields exist
        reading = f"{event.value} (>{event.threshold})"
    return f"⚠ {event.node_name} {label} {event.severity}: {reading}"
