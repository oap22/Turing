"""Tests for transport.envelope.MeshMessage and replay protection."""

from __future__ import annotations

import pytest

from turing.transport.envelope import MeshMessage, ReplayError, ReplayWindow
from turing.transport.signer import MessageSigner, SignatureError


def test_mesh_message_serialises_and_round_trips() -> None:
    msg = MeshMessage(
        request_id="req-1",
        sender_id="coordinator",
        subject="echo.request",
        payload=b"hello",
        timestamp_ms=1_700_000_000_000,
    )

    raw = msg.to_bytes()
    restored = MeshMessage.from_bytes(raw)

    assert restored == msg


def test_signed_envelope_round_trip_recovers_message() -> None:
    signer = MessageSigner.generate()
    msg = MeshMessage(
        request_id="req-1",
        sender_id="coordinator",
        subject="echo.request",
        payload=b"hello",
        timestamp_ms=1_700_000_000_000,
    )

    signed = signer.sign(msg.to_bytes())
    restored = MeshMessage.from_bytes(
        signer.verify(signed, trusted_public_keys=[signer.public_key])
    )

    assert restored == msg


def test_signed_envelope_rejects_tampered_subject() -> None:
    signer = MessageSigner.generate()
    msg = MeshMessage(
        request_id="req-1",
        sender_id="coordinator",
        subject="echo.request",
        payload=b"hello",
        timestamp_ms=1_700_000_000_000,
    )

    signed = signer.sign(msg.to_bytes())
    tampered_msg = MeshMessage(
        request_id="req-1",
        sender_id="coordinator",
        subject="shell.exec",  # subject swapped after signing
        payload=b"hello",
        timestamp_ms=1_700_000_000_000,
    )
    tampered = signed.with_payload(tampered_msg.to_bytes())

    with pytest.raises(SignatureError):
        signer.verify(tampered, trusted_public_keys=[signer.public_key])


def test_replay_window_first_seen_passes() -> None:
    window = ReplayWindow(ttl_ms=5_000, now_ms=lambda: 1_000)
    window.observe(request_id="req-1", timestamp_ms=900)


def test_replay_window_rejects_duplicate_within_ttl() -> None:
    window = ReplayWindow(ttl_ms=5_000, now_ms=lambda: 1_000)
    window.observe(request_id="req-1", timestamp_ms=900)

    with pytest.raises(ReplayError):
        window.observe(request_id="req-1", timestamp_ms=900)


def test_replay_window_rejects_message_older_than_ttl() -> None:
    window = ReplayWindow(ttl_ms=5_000, now_ms=lambda: 10_000)

    with pytest.raises(ReplayError):
        window.observe(request_id="req-old", timestamp_ms=1_000)


def test_replay_window_forgets_after_ttl_so_id_can_be_reused_in_distant_future() -> None:
    clock = {"t": 1_000}
    window = ReplayWindow(ttl_ms=5_000, now_ms=lambda: clock["t"])
    window.observe(request_id="req-1", timestamp_ms=1_000)

    clock["t"] = 20_000
    # The original record has expired; a *new* message with the same id and a
    # fresh timestamp is allowed.
    window.observe(request_id="req-1", timestamp_ms=19_000)


def test_replay_window_still_rejects_replay_of_a_reused_id() -> None:
    """A re-observed id must stay protected, not fall through a stale record.

    Eviction is driven by a heap ordered on timestamp. When an id expires and
    is then observed again, the heap briefly holds a record for the *old*
    timestamp. If eviction acted on that record it would forget the new,
    still-live observation and let it be replayed — so this guards the
    freshness boundary, not just the speed of it.
    """
    clock = {"t": 1_000}
    window = ReplayWindow(ttl_ms=5_000, now_ms=lambda: clock["t"])
    window.observe(request_id="req-1", timestamp_ms=1_000)

    clock["t"] = 20_000
    window.observe(request_id="req-1", timestamp_ms=19_000)

    # The re-added observation is inside the window and must still be caught.
    clock["t"] = 20_001
    with pytest.raises(ReplayError):
        window.observe(request_id="req-1", timestamp_ms=19_000)


def test_replay_window_evicts_only_expired_ids() -> None:
    """Eviction must drop exactly the out-of-window ids and keep the rest."""
    clock = {"t": 10_000}
    window = ReplayWindow(ttl_ms=5_000, now_ms=lambda: clock["t"])

    for i in range(50):
        window.observe(request_id=f"old-{i}", timestamp_ms=6_000)
    for i in range(50):
        window.observe(request_id=f"new-{i}", timestamp_ms=9_500)

    # Advance so the 6_000-stamped ids fall out of the window but the
    # 9_500-stamped ones do not.
    clock["t"] = 12_000

    # Expired ids are forgotten, so re-observing one is allowed.
    window.observe(request_id="old-0", timestamp_ms=11_500)

    # Still-live ids remain protected.
    with pytest.raises(ReplayError):
        window.observe(request_id="new-0", timestamp_ms=9_500)
