"""Test θ: Alert.to_frame() matches the golden JSON fixture key-for-key.

The same fixture is consumed by the vitest test for the SPA reducer/banner,
so any Py↔TS drift on the frame contract fails *one* of them deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path

from turing.coordinator.alerts.types import Alert

_FIXTURE = Path(__file__).parent / "fixtures" / "alert_frames_golden.json"


def _build_alerting() -> Alert:
    return Alert(
        node_id="pi-beta",
        node_name="pi-beta",
        field="temp_celsius",
        severity="danger",
        value=83.4,
        threshold=82.0,
        state="alerting",
        fired_at_ms=1_700_000_000_000,
    )


def _build_cleared() -> Alert:
    return Alert(
        node_id="pi-beta",
        node_name="pi-beta",
        field="temp_celsius",
        severity="danger",
        value=50.0,
        threshold=82.0,
        state="cleared",
        fired_at_ms=1_700_000_060_000,
    )


def test_alerting_frame_matches_golden() -> None:
    expected = json.loads(_FIXTURE.read_text())["alerting"]
    assert _build_alerting().to_frame() == expected


def test_cleared_frame_matches_golden() -> None:
    expected = json.loads(_FIXTURE.read_text())["cleared"]
    assert _build_cleared().to_frame() == expected
