"""Test α: AlertEngine state-machine cases.

Pure state-machine — no I/O, no dispatcher. The engine returns the new state
and an optional ``Alert`` event each step.
"""

from __future__ import annotations

import pytest

from turing.coordinator.alerts.engine import AlertEngine
from turing.coordinator.alerts.types import (
    DISK_DANGER,
    DISK_WARN,
    TEMP_DANGER,
    TEMP_WARN,
    Alert,
    AlertState,
)


def _engine() -> AlertEngine:
    return AlertEngine(now_ms=lambda: 0)


class TestAlertEngine:
    def test_ok_thrice_stays_clear(self) -> None:
        eng = _engine()
        events: list[Alert | None] = []
        for _ in range(3):
            _, ev = eng.step("peerA", "temp_celsius", "ok", value=20.0, threshold=TEMP_WARN)
            events.append(ev)
        assert eng.state_of("peerA", "temp_celsius") == AlertState.clear
        assert all(e is None for e in events)

    def test_warn_thrice_alerts_once(self) -> None:
        eng = _engine()
        states: list[AlertState] = []
        events: list[Alert | None] = []
        for _ in range(3):
            state, ev = eng.step("peerA", "temp_celsius", "warn", value=77.0, threshold=TEMP_WARN)
            states.append(state)
            events.append(ev)
        assert states == [AlertState.pending, AlertState.pending, AlertState.alerting]
        # Exactly one alerting event on the 3rd step.
        non_none = [e for e in events if e is not None]
        assert len(non_none) == 1
        assert non_none[0].state == "alerting"
        assert non_none[0].severity == "warn"
        assert non_none[0].value == 77.0

    def test_danger_twice_alerts_once(self) -> None:
        eng = _engine()
        ev1 = eng.step("p", "temp_celsius", "danger", value=85.0, threshold=TEMP_DANGER)[1]
        ev2 = eng.step("p", "temp_celsius", "danger", value=85.0, threshold=TEMP_DANGER)[1]
        assert ev1 is None
        assert ev2 is not None
        assert ev2.severity == "danger"
        assert ev2.state == "alerting"

    def test_mixed_warn_warn_danger_upgrades_to_danger(self) -> None:
        eng = _engine()
        eng.step("p", "temp_celsius", "warn", value=76.0, threshold=TEMP_WARN)
        eng.step("p", "temp_celsius", "warn", value=77.0, threshold=TEMP_WARN)
        # 2 warns → pending, then danger should trip (since N_DANGER=2 and we have 1
        # danger after 2 prior warns → 3 consecutive non-ok ≥ N_WARN=3 → alerting at warn level?).
        # Per the issue: "mixed warn-warn-danger upgrades to danger".
        _, ev = eng.step("p", "temp_celsius", "danger", value=85.0, threshold=TEMP_DANGER)
        assert ev is not None
        assert ev.severity == "danger"
        assert ev.state == "alerting"

    def test_alerting_then_three_ok_clears_with_one_event(self) -> None:
        eng = _engine()
        # Drive to alerting.
        for _ in range(3):
            eng.step("p", "temp_celsius", "warn", value=77.0, threshold=TEMP_WARN)
        assert eng.state_of("p", "temp_celsius") == AlertState.alerting

        # Two OK heartbeats do not yet clear.
        for _ in range(2):
            _, ev = eng.step("p", "temp_celsius", "ok", value=50.0, threshold=TEMP_WARN)
            assert ev is None
        assert eng.state_of("p", "temp_celsius") == AlertState.alerting

        # Third OK clears with exactly one ``cleared`` event.
        state, ev = eng.step("p", "temp_celsius", "ok", value=48.0, threshold=TEMP_WARN)
        assert state == AlertState.clear
        assert ev is not None
        assert ev.state == "cleared"

    def test_no_duplicate_alerting_events_within_a_run(self) -> None:
        eng = _engine()
        events: list[Alert] = []
        for _ in range(10):
            _, ev = eng.step("p", "temp_celsius", "warn", value=77.0, threshold=TEMP_WARN)
            if ev is not None:
                events.append(ev)
        assert len(events) == 1
        assert events[0].state == "alerting"

    def test_warn_then_ok_resets_pending(self) -> None:
        eng = _engine()
        eng.step("p", "temp_celsius", "warn", value=76.0, threshold=TEMP_WARN)
        eng.step("p", "temp_celsius", "warn", value=76.0, threshold=TEMP_WARN)
        # Reset.
        _, ev = eng.step("p", "temp_celsius", "ok", value=50.0, threshold=TEMP_WARN)
        assert ev is None
        assert eng.state_of("p", "temp_celsius") == AlertState.clear
        # Now need 3 warns again to alert.
        _, ev = eng.step("p", "temp_celsius", "warn", value=76.0, threshold=TEMP_WARN)
        assert ev is None
        _, ev = eng.step("p", "temp_celsius", "warn", value=76.0, threshold=TEMP_WARN)
        assert ev is None
        _, ev = eng.step("p", "temp_celsius", "warn", value=76.0, threshold=TEMP_WARN)
        assert ev is not None


@pytest.mark.parametrize("severity,threshold", [("warn", TEMP_WARN), ("danger", TEMP_DANGER)])
def test_alert_event_carries_severity_and_threshold(severity: str, threshold: float) -> None:
    eng = _engine()
    n = 3 if severity == "warn" else 2
    for _ in range(n):
        _, ev = eng.step("p", "temp_celsius", severity, value=threshold + 1.0, threshold=threshold)
    assert ev is not None
    assert ev.severity == severity
    assert ev.threshold == threshold


class TestAlertEngineDiskField:
    """Test α (DISK engine): the state machine behaves identically for the
    ``disk_pct`` field — the engine is field-agnostic, keyed by (peer, field)."""

    def test_disk_warn_thrice_alerts_once(self) -> None:
        eng = _engine()
        events: list[Alert | None] = []
        for _ in range(3):
            _, ev = eng.step("p", "disk_pct", "warn", value=88.0, threshold=DISK_WARN)
            events.append(ev)
        non_none = [e for e in events if e is not None]
        assert len(non_none) == 1
        assert non_none[0].field == "disk_pct"
        assert non_none[0].severity == "warn"
        assert non_none[0].state == "alerting"

    def test_disk_danger_twice_alerts_once(self) -> None:
        eng = _engine()
        ev1 = eng.step("p", "disk_pct", "danger", value=96.0, threshold=DISK_DANGER)[1]
        ev2 = eng.step("p", "disk_pct", "danger", value=96.0, threshold=DISK_DANGER)[1]
        assert ev1 is None
        assert ev2 is not None
        assert ev2.field == "disk_pct"
        assert ev2.severity == "danger"

    def test_disk_alerting_then_three_ok_clears(self) -> None:
        eng = _engine()
        for _ in range(3):
            eng.step("p", "disk_pct", "warn", value=88.0, threshold=DISK_WARN)
        assert eng.state_of("p", "disk_pct") == AlertState.alerting
        for _ in range(2):
            _, ev = eng.step("p", "disk_pct", "ok", value=10.0, threshold=DISK_WARN)
            assert ev is None
        state, ev = eng.step("p", "disk_pct", "ok", value=10.0, threshold=DISK_WARN)
        assert state == AlertState.clear
        assert ev is not None
        assert ev.state == "cleared"


def test_multi_field_independence() -> None:
    """Test α (multi-field independence): one peer, two fields, two state
    machines. DISK trips at danger×2, TEMP at warn×3; each clears on its own."""
    eng = _engine()
    temp_events: list[Alert] = []
    disk_events: list[Alert] = []

    # Three interleaved heartbeats — each steps both fields.
    for _ in range(3):
        _, te = eng.step("p", "temp_celsius", "warn", value=78.0, threshold=TEMP_WARN)
        _, de = eng.step("p", "disk_pct", "danger", value=96.0, threshold=DISK_WARN)
        if te is not None:
            temp_events.append(te)
        if de is not None:
            disk_events.append(de)

    assert len(temp_events) == 1, "TEMP should fire exactly once (warn×3)"
    assert temp_events[0].field == "temp_celsius"
    assert temp_events[0].severity == "warn"
    assert len(disk_events) == 1, "DISK should fire exactly once (danger×2)"
    assert disk_events[0].field == "disk_pct"
    assert disk_events[0].severity == "danger"

    # Each field clears independently after its own 3 ok heartbeats.
    for _ in range(2):
        _, te = eng.step("p", "temp_celsius", "ok", value=40.0, threshold=TEMP_WARN)
        _, de = eng.step("p", "disk_pct", "ok", value=10.0, threshold=DISK_WARN)
        assert te is None and de is None
    _, te = eng.step("p", "temp_celsius", "ok", value=40.0, threshold=TEMP_WARN)
    _, de = eng.step("p", "disk_pct", "ok", value=10.0, threshold=DISK_WARN)
    assert te is not None and te.state == "cleared" and te.field == "temp_celsius"
    assert de is not None and de.state == "cleared" and de.field == "disk_pct"
