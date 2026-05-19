"""Alert types + thresholds for the hardware-safety subsystem.

Constants here are the load-bearing copy of the TEMP thresholds; the parity
test ``tests/test_coordinator/test_alerts_thresholds.py`` reads
``webui/src/specs/thresholds.ts`` and asserts the two stay in lock-step so
Python and TypeScript can never drift.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass
from typing import Any, Literal

# TEMP thresholds — kept in sync with ``webui/src/specs/thresholds.ts``.
TEMP_WARN = 75.0
TEMP_DANGER = 82.0

# Enter-and-hold counters. Hardcoded per PRD #228; configuration is out of
# scope until a real operator asks for it.
N_WARN = 3
N_DANGER = 2

Severity = Literal["ok", "warn", "danger"]
Field = Literal["temp_celsius"]
AlertEventState = Literal["alerting", "cleared"]


class AlertState(enum.StrEnum):
    clear = "clear"
    pending = "pending"
    alerting = "alerting"


@dataclass(frozen=True)
class Alert:
    """A single alert frame — the WS payload shape (sans ``type`` discriminator).

    Slice 1 schema, deliberately minimal. ``snoozed_until_ms`` lands in slice 3.
    """

    node_id: str
    node_name: str
    field: Field
    severity: Severity
    value: float
    threshold: float
    state: AlertEventState
    fired_at_ms: int

    def to_frame(self) -> dict[str, Any]:
        """Serialise as the ``type: \"alert\"`` WebSocket frame."""
        frame: dict[str, Any] = {"type": "alert"}
        frame.update(asdict(self))
        return frame


def severity_for_temp(temp_celsius: float | None) -> Severity:
    """Python mirror of ``severityFor`` from ``webui/src/specs/thresholds.ts``.

    TEMP only in slice 1. A ``None`` reading is ``ok`` — a missing sample
    is operationally identical to a fine sample for the alert engine.
    """
    if temp_celsius is None:
        return "ok"
    if temp_celsius >= TEMP_DANGER:
        return "danger"
    if temp_celsius >= TEMP_WARN:
        return "warn"
    return "ok"
