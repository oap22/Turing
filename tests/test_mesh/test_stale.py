"""Tests for ``turing.mesh.node.is_specs_stale``.

Pure function, time injected for determinism. Slice 3/3 (#217) of the
fleet specs panel uses this on the backend so the gateway and the SPA
agree on which peers should dim in the UI.
"""

from __future__ import annotations

from turing.mesh.node import STALE_AFTER_SECONDS, PeerInfo, is_specs_stale


def _peer(last_seen: float) -> PeerInfo:
    return PeerInfo(node_id="x", name="x", last_seen=last_seen)


class TestIsSpecsStale:
    def test_fresh_peer_is_not_stale(self) -> None:
        peer = _peer(last_seen=1_000_000.0)
        assert is_specs_stale(peer, now=1_000_000.0 + 1.0) is False

    def test_just_below_threshold_not_stale(self) -> None:
        peer = _peer(last_seen=1_000_000.0)
        assert is_specs_stale(peer, now=1_000_000.0 + (STALE_AFTER_SECONDS - 0.01)) is False

    def test_at_threshold_not_stale(self) -> None:
        # The check is ``>``, not ``>=``, so the exact threshold tick is still alive.
        peer = _peer(last_seen=1_000_000.0)
        assert is_specs_stale(peer, now=1_000_000.0 + STALE_AFTER_SECONDS) is False

    def test_past_threshold_is_stale(self) -> None:
        peer = _peer(last_seen=1_000_000.0)
        assert is_specs_stale(peer, now=1_000_000.0 + STALE_AFTER_SECONDS + 1.0) is True

    def test_well_past_threshold_is_stale(self) -> None:
        peer = _peer(last_seen=1_000_000.0)
        assert is_specs_stale(peer, now=1_000_000.0 + 3600.0) is True

    def test_custom_stale_after_is_respected(self) -> None:
        peer = _peer(last_seen=1_000_000.0)
        assert is_specs_stale(peer, now=1_000_000.0 + 5.0, stale_after=2.0) is True
        assert is_specs_stale(peer, now=1_000_000.0 + 1.0, stale_after=2.0) is False

    def test_now_defaults_to_real_clock(self) -> None:
        # last_seen far in the past → must read as stale even without injecting now.
        peer = _peer(last_seen=0.0)
        assert is_specs_stale(peer) is True
