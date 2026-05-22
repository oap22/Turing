"""Hardware-safety alerts subsystem.

Per PRD #228: watch each peer's ``specs.temp_celsius`` and ``disk_pct``,
detect sustained warn/danger conditions through a small state machine, and
push ``alert`` frames out over the existing gateway WebSocket so the SPA
can render a persistent banner.

Discord fallback and snooze land in later slices.
"""

from turing.coordinator.alerts.dispatcher import DEFAULT_SNOOZE_MS, AlertDispatcher
from turing.coordinator.alerts.engine import AlertEngine
from turing.coordinator.alerts.types import (
    DISK_DANGER,
    DISK_WARN,
    KNOWN_FIELDS,
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
    "DEFAULT_SNOOZE_MS",
    "DISK_DANGER",
    "DISK_WARN",
    "KNOWN_FIELDS",
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
