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

# TEMP / DISK thresholds — kept in sync with ``webui/src/specs/thresholds.ts``.
TEMP_WARN = 75.0
TEMP_DANGER = 82.0
DISK_WARN = 85.0
DISK_DANGER = 95.0

# Enter-and-hold counters. Hardcoded per PRD #228; configuration is out of
# scope until a real operator asks for it.
N_WARN = 3
N_DANGER = 2

Severity = Literal["ok", "warn", "danger"]
Field = Literal["temp_celsius", "disk_pct"]
AlertEventState = Literal["alerting", "cleared"]

# The set of fields the engine knows how to grade. The snooze endpoint
# rejects anything outside it (catches typos like ``disk_used``).
KNOWN_FIELDS: frozenset[str] = frozenset({"temp_celsius", "disk_pct"})


class AlertState(enum.StrEnum):
    clear = "clear"
    pending = "pending"
    alerting = "alerting"


@dataclass(frozen=True)
class Alert:
    """A single alert frame — the WS payload shape (sans ``type`` discriminator).

    ``snoozed_until_ms`` is ``None`` on a normal ``alerting`` / ``cleared``
    edge; it carries an epoch-ms expiry only on a snooze ``update`` frame
    (state ``alerting``, emitted when the operator snoozes an active alert).
    """

    node_id: str
    node_name: str
    field: Field
    severity: Severity
    value: float
    threshold: float
    state: AlertEventState
    fired_at_ms: int
    snoozed_until_ms: int | None = None

    def to_frame(self) -> dict[str, Any]:
        """Serialise as the ``type: \"alert\"`` WebSocket frame."""
        frame: dict[str, Any] = {"type": "alert"}
        frame.update(asdict(self))
        return frame


def severity_for_temp(temp_celsius: float | None) -> Severity:
    """Python mirror of the TEMP grading in ``webui/src/specs/thresholds.ts``.

    A ``None`` reading is ``ok`` — a missing sample is operationally
    identical to a fine sample for the alert engine.
    """
    if temp_celsius is None:
        return "ok"
    if temp_celsius >= TEMP_DANGER:
        return "danger"
    if temp_celsius >= TEMP_WARN:
        return "warn"
    return "ok"


def disk_percent(used_bytes: float | None, total_bytes: float | None) -> float | None:
    """Disk utilisation as a percentage.

    Returns ``None`` when ``total_bytes`` is missing or zero — the
    collector's sentinel for "disk collection failed on this platform".
    A ``None`` result reads as ``ok`` (see :func:`severity_for_disk`).
    """
    if not total_bytes or total_bytes <= 0:
        return None
    return 100.0 * (used_bytes or 0.0) / total_bytes


def severity_for_disk(disk_pct: float | None) -> Severity:
    """Python mirror of the DISK grading in ``webui/src/specs/thresholds.ts``.

    A ``None`` percentage (unknown / zero disk total) is treated as ``ok``.
    """
    if disk_pct is None:
        return "ok"
    if disk_pct >= DISK_DANGER:
        return "danger"
    if disk_pct >= DISK_WARN:
        return "warn"
    return "ok"
