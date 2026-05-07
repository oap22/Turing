"""Tests for SignedTransport — signs on publish, verifies + replay-checks on receive.

Uses an in-memory bus fake; the NATS-backed integration test lives in
`tests/integration/test_transport_nats.py` and is opt-in via the
`integration` marker.
"""

from __future__ import annotations

import pytest

from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage, ReplayError
from turing.transport.signed_transport import SignedTransport, UntrustedSenderError
from turing.transport.signer import MessageSigner, SignatureError


def _msg(request_id: str = "req-1", subject: str = "echo.request") -> MeshMessage:
    return MeshMessage(
        request_id=request_id,
        sender_id="coordinator",
        subject=subject,
        payload=b"hello",
        timestamp_ms=1_000,
    )


@pytest.fixture()
def bus() -> InMemoryBus:
    return InMemoryBus()


async def test_publish_then_receive_round_trips_a_message(bus: InMemoryBus) -> None:
    signer = MessageSigner.generate()
    transport = SignedTransport(
        bus=bus, signer=signer, trusted_keys=[signer.public_key], now_ms=lambda: 1_000
    )

    received: list[MeshMessage] = []
    await transport.subscribe("echo.request", received.append)
    await transport.publish(_msg())

    assert received == [_msg()]


async def test_subscribe_drops_messages_from_untrusted_senders(bus: InMemoryBus) -> None:
    alice = MessageSigner.generate()
    mallory = MessageSigner.generate()

    listener = SignedTransport(
        bus=bus, signer=alice, trusted_keys=[alice.public_key], now_ms=lambda: 1_000
    )
    attacker = SignedTransport(
        bus=bus, signer=mallory, trusted_keys=[mallory.public_key], now_ms=lambda: 1_000
    )

    received: list[MeshMessage] = []
    errors: list[Exception] = []
    await listener.subscribe("echo.request", received.append, on_error=errors.append)
    await attacker.publish(_msg())

    assert received == []
    assert any(isinstance(e, (SignatureError, UntrustedSenderError)) for e in errors)


async def test_subscribe_drops_replayed_messages(bus: InMemoryBus) -> None:
    signer = MessageSigner.generate()
    transport = SignedTransport(
        bus=bus,
        signer=signer,
        trusted_keys=[signer.public_key],
        now_ms=lambda: 1_000,
        replay_ttl_ms=5_000,
    )

    received: list[MeshMessage] = []
    errors: list[Exception] = []
    await transport.subscribe("echo.request", received.append, on_error=errors.append)

    await transport.publish(_msg())
    await transport.publish(_msg())  # same request_id — replay

    assert len(received) == 1
    assert any(isinstance(e, ReplayError) for e in errors)
