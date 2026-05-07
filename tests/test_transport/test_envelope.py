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
