"""Hardware-safety alerts subsystem.

Per PRD #228 / slice 1 #229: watch each peer's ``specs.temp_celsius`` (and
``disk_pct`` in slice 2), detect sustained warn/danger conditions through a
small state machine, and push ``alert`` frames out over the existing gateway
WebSocket so the SPA can render a persistent banner.

Discord fallback and snooze land in later slices; this slice is strictly
vertical and ships no forward-compatibility shims for them.
"""

from turing.coordinator.alerts.dispatcher import AlertDispatcher
from turing.coordinator.alerts.engine import AlertEngine
from turing.coordinator.alerts.types import (
    TEMP_DANGER,
    TEMP_WARN,
    Alert,
    AlertState,
    Field,
    Severity,
    severity_for_temp,
)

__all__ = [
    "TEMP_DANGER",
    "TEMP_WARN",
    "Alert",
    "AlertDispatcher",
    "AlertEngine",
    "AlertState",
    "Field",
    "Severity",
    "severity_for_temp",
]
