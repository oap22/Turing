"""Hardware-safety alerts subsystem.

Per PRD #228: watch each peer's ``specs.temp_celsius`` and ``disk_pct``,
detect sustained warn/danger conditions through a small state machine, and
push ``alert`` frames out over the existing gateway WebSocket so the SPA
can render a persistent banner.

Discord fallback and snooze land in later slices.
"""

from turing.coordinator.alerts.dispatcher import AlertDispatcher
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

__all__ = [
    "DISK_DANGER",
    "DISK_WARN",
    "TEMP_DANGER",
    "TEMP_WARN",
    "Alert",
    "AlertDispatcher",
    "AlertEngine",
    "AlertState",
    "Field",
    "Severity",
    "disk_percent",
    "severity_for_disk",
    "severity_for_temp",
]
