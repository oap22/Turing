"""Pure state machine for hardware-safety alerts.

One state machine per ``(peer_id, field)``. Driven by ``step`` calls with the
current ``Severity`` for that field; returns the new ``AlertState`` plus an
optional ``Alert`` event (only on transitions that the operator should hear
about: ``→ alerting`` and ``→ clear`` after an alerting run).

Counter semantics — "enter and hold":

* ``clear → pending → alerting`` requires N consecutive warn-or-worse
  heartbeats: ``N_WARN=3`` for ``warn``, ``N_DANGER=2`` for ``danger``.
* A single ``ok`` heartbeat in the run resets the counter back to clear
  (the run was not sustained).
* ``alerting → clear`` requires N_WARN consecutive ``ok`` heartbeats so a
  brief dip below threshold doesn't ping-pong the banner.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from turing.coordinator.alerts.types import (
    N_DANGER,
    N_WARN,
    Alert,
    AlertState,
    Field,
    Severity,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class _Counter:
    __slots__ = ("consecutive", "last_severity", "state")

    def __init__(self) -> None:
        self.state: AlertState = AlertState.clear
        self.consecutive: int = 0
        self.last_severity: Severity = "ok"


class AlertEngine:
    """In-memory state machine keyed by ``(node_id, field)``.

    ``now_ms`` is injectable so tests get deterministic ``fired_at_ms``.
    """

    def __init__(self, *, now_ms: Callable[[], int] | None = None) -> None:
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._counters: dict[tuple[str, Field], _Counter] = {}

    def state_of(self, node_id: str, field: Field) -> AlertState:
        return self._counters.get((node_id, field), _Counter()).state

    def step(
        self,
        node_id: str,
        field: Field,
        severity: Severity,
        *,
        value: float,
        threshold: float,
        node_name: str | None = None,
    ) -> tuple[AlertState, Alert | None]:
        """Advance the (node_id, field) state machine and maybe emit one event.

        Returns ``(new_state, event_or_None)``. Only state *transitions*
        worth telling the operator about emit an event:

        - ``pending → alerting`` when the warn/danger run hits N
        - ``alerting → clear`` when the ok run hits N_WARN

        Every other input — including additional warn heartbeats during an
        already-``alerting`` run — returns ``None``.
        """
        key = (node_id, field)
        counter = self._counters.setdefault(key, _Counter())
        name = node_name or node_id

        if counter.state == AlertState.alerting:
            return self._step_in_alerting(counter, severity, node_id, name, field, value, threshold)

        # clear / pending — both watch for a warn-or-worse run.
        if severity == "ok":
            counter.state = AlertState.clear
            counter.consecutive = 0
            counter.last_severity = "ok"
            return counter.state, None

        # warn or danger: start or extend the run. Keep the worst severity
        # seen so the eventual ``alerting`` event reports the right level.
        counter.consecutive += 1
        counter.last_severity = _worst(counter.last_severity, severity)

        # Danger trips at 2 heartbeats regardless of severity ordering; warn
        # trips at 3 heartbeats.
        threshold_count = N_DANGER if counter.last_severity == "danger" else N_WARN
        if counter.consecutive >= threshold_count:
            counter.state = AlertState.alerting
            event = Alert(
                node_id=node_id,
                node_name=name,
                field=field,
                severity=counter.last_severity,
                value=value,
                threshold=threshold,
                state="alerting",
                fired_at_ms=self._now_ms(),
            )
            # Reset counter so the cleared run starts fresh.
            counter.consecutive = 0
            return counter.state, event

        counter.state = AlertState.pending
        return counter.state, None

    def _step_in_alerting(
        self,
        counter: _Counter,
        severity: Severity,
        node_id: str,
        node_name: str,
        field: Field,
        value: float,
        threshold: float,
    ) -> tuple[AlertState, Alert | None]:
        if severity == "ok":
            counter.consecutive += 1
            if counter.consecutive >= N_WARN:
                counter.state = AlertState.clear
                counter.consecutive = 0
                last_sev = counter.last_severity
                counter.last_severity = "ok"
                event = Alert(
                    node_id=node_id,
                    node_name=node_name,
                    field=field,
                    severity=last_sev,
                    value=value,
                    threshold=threshold,
                    state="cleared",
                    fired_at_ms=self._now_ms(),
                )
                return counter.state, event
            return counter.state, None
        # Any non-ok heartbeat while alerting: stay alerting, reset the
        # clear counter, refresh the worst-seen severity.
        counter.consecutive = 0
        counter.last_severity = _worst(counter.last_severity, severity)
        return counter.state, None


_SEV_ORDER: dict[Severity, int] = {"ok": 0, "warn": 1, "danger": 2}


def _worst(a: Severity, b: Severity) -> Severity:
    return a if _SEV_ORDER[a] >= _SEV_ORDER[b] else b
