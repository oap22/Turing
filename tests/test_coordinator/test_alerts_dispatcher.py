"""Test β: AlertDispatcher fan-out.

Fakes the telemetry sink with a list-appender and the peer with a tiny
SimpleNamespace. Asserts exactly one ``alerting`` frame on the danger edge,
zero frames during steady alerting, and exactly one ``cleared`` frame on
return-to-ok.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from turing.coordinator.alerts.dispatcher import AlertDispatcher
from turing.coordinator.alerts.engine import AlertEngine


def _peer(temp_celsius: float | None) -> Any:
    specs = SimpleNamespace(temp_celsius=temp_celsius)
    return SimpleNamespace(node_id="pi-beta", name="pi-beta", specs=specs)


def _disk_peer(disk_used_bytes: int, disk_total_bytes: int) -> Any:
    specs = SimpleNamespace(
        disk_used_bytes=disk_used_bytes,
        disk_total_bytes=disk_total_bytes,
    )
    return SimpleNamespace(node_id="pi-gamma", name="pi-gamma", specs=specs)


def _full_peer(
    node_id: str,
    *,
    temp_celsius: float | None = None,
    disk_used_bytes: int = 0,
    disk_total_bytes: int = 100,
) -> Any:
    """A peer carrying both alertable fields, for snooze cross-field tests."""
    specs = SimpleNamespace(
        temp_celsius=temp_celsius,
        disk_used_bytes=disk_used_bytes,
        disk_total_bytes=disk_total_bytes,
    )
    return SimpleNamespace(node_id=node_id, name=node_id, specs=specs)


@pytest.mark.asyncio
async def test_alerting_and_cleared_each_emit_exactly_one_frame() -> None:
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    eng = AlertEngine(now_ms=lambda: 1_700_000_000_000)
    dispatcher = AlertDispatcher(eng, send_frame=sink)

    # 1 hot heartbeat → no frame yet (warn × 1).
    await dispatcher.observe(_peer(76.0))
    assert frames == []

    # 2 more → exactly one alerting frame.
    await dispatcher.observe(_peer(77.0))
    await dispatcher.observe(_peer(78.0))
    assert len(frames) == 1
    assert frames[0]["type"] == "alert"
    assert frames[0]["state"] == "alerting"
    assert frames[0]["severity"] == "warn"
    assert frames[0]["field"] == "temp_celsius"
    assert frames[0]["node_id"] == "pi-beta"

    # Continued hot heartbeats during the alerting run: no further frames.
    for _ in range(5):
        await dispatcher.observe(_peer(78.0))
    assert len(frames) == 1

    # Three cool heartbeats: one cleared frame.
    await dispatcher.observe(_peer(50.0))
    assert len(frames) == 1
    await dispatcher.observe(_peer(50.0))
    assert len(frames) == 1
    await dispatcher.observe(_peer(50.0))
    assert len(frames) == 2
    assert frames[1]["state"] == "cleared"


@pytest.mark.asyncio
async def test_no_sink_no_explosion() -> None:
    """Dispatcher with no ``send_frame`` doesn't raise; engine still advances."""
    dispatcher = AlertDispatcher(AlertEngine(now_ms=lambda: 0))
    for _ in range(3):
        await dispatcher.observe(_peer(80.0))
    # Engine state should still be ``alerting``.
    assert dispatcher.engine.state_of("pi-beta", "temp_celsius").value == "alerting"


@pytest.mark.asyncio
async def test_observe_skips_when_specs_missing() -> None:
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    dispatcher = AlertDispatcher(send_frame=sink)
    peer = SimpleNamespace(node_id="x", name="x", specs=None)
    await dispatcher.observe(peer)
    assert frames == []


@pytest.mark.asyncio
async def test_observe_swallows_sink_errors() -> None:
    """A flaky WS subscriber must not break the heartbeat handler."""

    async def sink(_frame: dict) -> None:
        raise RuntimeError("ws closed")

    dispatcher = AlertDispatcher(send_frame=sink)
    # Drive into alerting — this is where the sink call happens.
    for _ in range(3):
        await dispatcher.observe(_peer(80.0))
    # Surviving the loop is the assertion.


@pytest.mark.asyncio
async def test_disk_danger_emits_one_alert_frame() -> None:
    """A peer past DISK_DANGER fires exactly one ``disk_pct`` frame."""
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    dispatcher = AlertDispatcher(AlertEngine(now_ms=lambda: 1_700_000_000_000), send_frame=sink)
    # 96 / 100 bytes → 96 % disk usage → danger. N_DANGER = 2.
    hot = _disk_peer(disk_used_bytes=96, disk_total_bytes=100)
    await dispatcher.observe(hot)
    assert frames == []
    await dispatcher.observe(hot)
    assert len(frames) == 1
    assert frames[0]["field"] == "disk_pct"
    assert frames[0]["severity"] == "danger"
    assert frames[0]["state"] == "alerting"
    assert frames[0]["value"] == 96.0
    assert frames[0]["threshold"] == 95.0


@pytest.mark.asyncio
async def test_zero_disk_total_never_alerts() -> None:
    """Test δ: ``disk_total_bytes == 0`` is the collector's sentinel for
    'collection failed on this platform' — it must never produce a disk
    alert, no matter how large ``disk_used_bytes`` is."""
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    dispatcher = AlertDispatcher(send_frame=sink)
    peer = _disk_peer(disk_used_bytes=10**12, disk_total_bytes=0)
    for _ in range(10):
        await dispatcher.observe(peer)
    assert frames == []
    assert dispatcher.engine.state_of("pi-gamma", "disk_pct").value == "clear"


_NOW = 1_700_000_000_000
_FOUR_H_MS = 4 * 60 * 60 * 1000


@pytest.mark.asyncio
async def test_snooze_suppresses_alerting_but_not_cleared() -> None:
    """Test β (snooze suppresses frames): a snooze silences the *alerting*
    edge for its (peer, field) while a different field keeps firing; a
    ``cleared`` edge is never suppressed (a resolved alert always clears the
    banner); after expiry the alerting edge fires again."""
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    dispatcher = AlertDispatcher(AlertEngine(now_ms=lambda: _NOW), send_frame=sink)

    # Snooze TEMP on pi-beta before it ever alerts → no update frame.
    expiry = await dispatcher.snooze("pi-beta", "temp_celsius", _NOW)
    assert frames == []

    # Within the window: TEMP danger×3 (alerting edge — suppressed) +
    # DISK danger×3 (not snoozed — fires once).
    for _ in range(3):
        await dispatcher.observe(_full_peer("pi-beta", temp_celsius=85.0, disk_used_bytes=99), _NOW)
    assert [(f["field"], f["state"]) for f in frames] == [("disk_pct", "alerting")]

    # TEMP recovers: ok×3 → cleared edge. A cleared is good news and is NOT
    # suppressed by the live snooze — the banner row must clear.
    for _ in range(3):
        await dispatcher.observe(_full_peer("pi-beta", temp_celsius=20.0, disk_used_bytes=99), _NOW)
    temp_frames = [f for f in frames if f["field"] == "temp_celsius"]
    assert len(temp_frames) == 1
    assert temp_frames[0]["state"] == "cleared"

    # After expiry: re-drive TEMP into alerting — the alerting edge lands.
    after = expiry + 1
    for _ in range(3):
        await dispatcher.observe(
            _full_peer("pi-beta", temp_celsius=85.0, disk_used_bytes=99), after
        )
    temp_alerting = [f for f in frames if f["field"] == "temp_celsius" and f["state"] == "alerting"]
    assert len(temp_alerting) == 1


@pytest.mark.asyncio
async def test_snooze_of_active_alert_emits_update_frame() -> None:
    """Test β (snooze of active alert emits update): snoozing an already
    ``alerting`` key emits one ``update`` frame — state alerting, non-null
    ``snoozed_until_ms`` matching the dispatcher's stored expiry."""
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    dispatcher = AlertDispatcher(AlertEngine(now_ms=lambda: _NOW), send_frame=sink)
    for _ in range(3):
        await dispatcher.observe(_peer(85.0), _NOW)
    assert len(frames) == 1
    assert frames[0]["state"] == "alerting"

    expiry = await dispatcher.snooze("pi-beta", "temp_celsius", _NOW)
    assert len(frames) == 2
    update = frames[1]
    assert update["type"] == "alert"
    assert update["state"] == "alerting"
    assert update["field"] == "temp_celsius"
    assert update["snoozed_until_ms"] == expiry
    assert expiry == _NOW + _FOUR_H_MS


@pytest.mark.asyncio
async def test_snooze_map_is_in_memory_only() -> None:
    """Test β (in-memory only, by-construction): a fresh dispatcher carries
    no snoozes — locks the contract against an accidental SQLite store."""
    d1 = AlertDispatcher(AlertEngine(now_ms=lambda: _NOW))
    await d1.snooze("pi-beta", "temp_celsius", _NOW)
    assert d1.snoozed_until("pi-beta", "temp_celsius") is not None

    # Throw d1 away; a brand-new dispatcher knows nothing of its snooze.
    d2 = AlertDispatcher(AlertEngine(now_ms=lambda: _NOW))
    assert d2.snoozed_until("pi-beta", "temp_celsius") is None


class _FakeNtfy:
    """Records push attempts; optionally raises to exercise failure isolation."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[str] = []
        self._raises = raises

    async def ntfy_push(self, content: str) -> None:
        self.calls.append(content)
        if self._raises:
            raise RuntimeError("503 ntfy unreachable")


@pytest.mark.asyncio
async def test_ntfy_fallback_engaged_when_spa_stale() -> None:
    """Test α (fallback engaged when stale)."""
    ntfy = _FakeNtfy()
    dispatcher = AlertDispatcher(
        AlertEngine(now_ms=lambda: _NOW),
        ntfy_client=ntfy,
        reachability_clock=lambda: _NOW - 120_000,
    )
    for _ in range(3):
        await dispatcher.observe(_peer(85.0), _NOW)
    assert ntfy.calls == ["⚠ pi-beta TEMP danger: 85.0°C (>82.0°C)"]


@pytest.mark.asyncio
async def test_ntfy_fallback_engaged_when_reachability_none() -> None:
    """Test α (fallback engaged when None — bootstrap-as-unreachable)."""
    ntfy = _FakeNtfy()
    dispatcher = AlertDispatcher(
        AlertEngine(now_ms=lambda: _NOW),
        ntfy_client=ntfy,
        reachability_clock=lambda: None,
    )
    for _ in range(3):
        await dispatcher.observe(_peer(85.0), _NOW)
    assert len(ntfy.calls) == 1


@pytest.mark.asyncio
async def test_ntfy_fallback_skipped_when_spa_fresh() -> None:
    """Test α (fallback skipped when fresh): a recent SPA push suppresses ntfy."""
    ntfy = _FakeNtfy()
    dispatcher = AlertDispatcher(
        AlertEngine(now_ms=lambda: _NOW),
        ntfy_client=ntfy,
        reachability_clock=lambda: _NOW - 30_000,
    )
    for _ in range(3):
        await dispatcher.observe(_peer(85.0), _NOW)
    assert ntfy.calls == []


@pytest.mark.asyncio
async def test_ntfy_cleared_never_posts() -> None:
    """Test α (cleared never posts): the push fires on the alerting edge only."""
    ntfy = _FakeNtfy()
    dispatcher = AlertDispatcher(
        AlertEngine(now_ms=lambda: _NOW),
        ntfy_client=ntfy,
        reachability_clock=lambda: _NOW - 120_000,
    )
    for _ in range(3):
        await dispatcher.observe(_peer(85.0), _NOW)  # alerting edge
    assert len(ntfy.calls) == 1
    for _ in range(3):
        await dispatcher.observe(_peer(20.0), _NOW)  # cleared edge
    assert len(ntfy.calls) == 1  # cleared did not push


@pytest.mark.asyncio
async def test_ntfy_snooze_suppresses_post() -> None:
    """Test α (snooze suppresses ntfy too)."""
    ntfy = _FakeNtfy()
    dispatcher = AlertDispatcher(
        AlertEngine(now_ms=lambda: _NOW),
        ntfy_client=ntfy,
        reachability_clock=lambda: None,
    )
    await dispatcher.snooze("pi-beta", "temp_celsius", _NOW)
    for _ in range(5):
        await dispatcher.observe(_peer(85.0), _NOW)
    assert ntfy.calls == []


@pytest.mark.asyncio
async def test_ntfy_failure_isolated_from_engine() -> None:
    """Test α (ntfy failure isolation): a raising ntfy_push never
    propagates out of observe; SPA frames still emit in order."""
    ntfy = _FakeNtfy(raises=True)
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    dispatcher = AlertDispatcher(
        AlertEngine(now_ms=lambda: _NOW),
        send_frame=sink,
        ntfy_client=ntfy,
        reachability_clock=lambda: None,
    )
    for _ in range(3):
        await dispatcher.observe(_peer(85.0), _NOW)  # alerting
    for _ in range(3):
        await dispatcher.observe(_peer(20.0), _NOW)  # cleared
    for _ in range(3):
        await dispatcher.observe(_peer(85.0), _NOW)  # re-alert

    # The engine's SPA frames are unaffected by the ntfy failures.
    assert [f["state"] for f in frames] == ["alerting", "cleared", "alerting"]
    # ntfy_push was attempted on each alerting edge and raised each time;
    # surviving the loop is the isolation guarantee.
    assert len(ntfy.calls) == 2


@pytest.mark.asyncio
async def test_snooze_survives_ntfy_failure() -> None:
    """A raising ntfy push on a live alerting edge must not clear or corrupt
    the snooze map or the engine's in-memory state.

    This drives the failure through the *transport* itself. The key alerts
    while *not* snoozed, so ``observe`` reaches ``_maybe_push`` and the raising
    ``ntfy_push`` actually fires (one attempt, which raises and is swallowed —
    failure isolation). The operator then snoozes the active alert. We assert
    the swallowed failure left everything intact: the snooze is recorded
    despite the earlier raise, the engine still reads ``alerting``, and a later
    heartbeat keeps respecting the snooze (no new push, no escaped frame)."""
    ntfy = _FakeNtfy(raises=True)
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    dispatcher = AlertDispatcher(
        AlertEngine(now_ms=lambda: _NOW),
        send_frame=sink,
        ntfy_client=ntfy,
        reachability_clock=lambda: None,  # unreachable → fallback engaged
    )

    # Alert first: the alerting edge fires one ntfy push, which raises and is
    # swallowed (failure isolation). The engine state is now ``alerting``.
    for _ in range(3):
        await dispatcher.observe(_peer(85.0), _NOW)
    assert len(ntfy.calls) == 1  # the raising push was attempted
    assert [f["state"] for f in frames] == ["alerting"]
    assert dispatcher.engine.state_of("pi-beta", "temp_celsius").value == "alerting"

    # Now snooze the active alert. The snooze is recorded regardless of the
    # earlier transport failure.
    expiry = await dispatcher.snooze("pi-beta", "temp_celsius", _NOW)
    assert dispatcher.snoozed_until("pi-beta", "temp_celsius") == expiry

    # The snooze map holds, and a fresh heartbeat keeps respecting it: no new
    # alerting frame, no new push (the alerting edge stays suppressed).
    for _ in range(5):
        await dispatcher.observe(_peer(85.0), _NOW)
    assert dispatcher.snoozed_until("pi-beta", "temp_celsius") == expiry
    assert dispatcher.engine.state_of("pi-beta", "temp_celsius").value == "alerting"
    assert len(ntfy.calls) == 1  # no further push after the snooze
    # Only the original alerting + the snooze update frame exist.
    assert [f["state"] for f in frames] == ["alerting", "alerting"]
    assert frames[1]["snoozed_until_ms"] == expiry


@pytest.mark.asyncio
async def test_no_ntfy_client_is_a_silent_noop() -> None:
    """Test β (config gate, dispatcher side): with no ntfy_client the
    alerting edge drives no ntfy interaction and never raises."""
    frames: list[dict] = []

    async def sink(frame: dict) -> None:
        frames.append(frame)

    dispatcher = AlertDispatcher(
        AlertEngine(now_ms=lambda: _NOW),
        send_frame=sink,
        reachability_clock=lambda: None,
    )
    for _ in range(3):
        await dispatcher.observe(_peer(85.0), _NOW)
    assert [f["state"] for f in frames] == ["alerting"]
