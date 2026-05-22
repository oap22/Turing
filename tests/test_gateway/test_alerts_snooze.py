"""Test δ: the POST /alerts/{node_id}/{field}/snooze gateway endpoint.

Covers bearer-auth enforcement, the happy path (returns an expiry ~4 h
out), and the two 404 cases — an unknown field (typo guard) and a
(peer, field) pair the alert engine has never graded.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from turing.coordinator.alerts.dispatcher import AlertDispatcher
from turing.coordinator.alerts.engine import AlertEngine
from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth

_FOUR_H_MS = 4 * 60 * 60 * 1000
_HEADERS = {"Authorization": "Bearer t0k3n"}


def _dispatcher_with_temp_state() -> AlertDispatcher:
    """A dispatcher whose engine has graded (pi-beta, temp_celsius) at least
    once — enough for ``has_state`` to return True."""
    engine = AlertEngine(now_ms=lambda: 1_700_000_000_000)
    engine.step("pi-beta", "temp_celsius", "danger", value=85.0, threshold=82.0)
    return AlertDispatcher(engine)


def _client(dispatcher: AlertDispatcher) -> TestClient:
    app = create_app(
        auth=GatewayAuth(token="t0k3n"),  # type: ignore[call-arg]
        node_name="pi-alpha",
        alert_dispatcher=dispatcher,
    )
    return TestClient(app)


def test_snooze_requires_bearer_auth() -> None:
    client = _client(_dispatcher_with_temp_state())
    res = client.post("/alerts/pi-beta/temp_celsius/snooze")
    assert res.status_code == 401


def test_snooze_valid_call_returns_expiry_about_four_hours_out() -> None:
    dispatcher = _dispatcher_with_temp_state()
    client = _client(dispatcher)
    before = int(time.time() * 1000)
    res = client.post("/alerts/pi-beta/temp_celsius/snooze", headers=_HEADERS)
    after = int(time.time() * 1000)
    assert res.status_code == 200
    snoozed_until_ms = res.json()["snoozed_until_ms"]
    assert before + _FOUR_H_MS <= snoozed_until_ms <= after + _FOUR_H_MS
    # The dispatcher recorded exactly what it returned.
    assert dispatcher.snoozed_until("pi-beta", "temp_celsius") == snoozed_until_ms


def test_snooze_unknown_field_returns_404() -> None:
    client = _client(_dispatcher_with_temp_state())
    # ``disk_used`` is a plausible typo for ``disk_pct`` — must 404.
    res = client.post("/alerts/pi-beta/disk_used/snooze", headers=_HEADERS)
    assert res.status_code == 404


def test_snooze_unknown_peer_returns_404() -> None:
    client = _client(_dispatcher_with_temp_state())
    res = client.post("/alerts/pi-nope/temp_celsius/snooze", headers=_HEADERS)
    assert res.status_code == 404
