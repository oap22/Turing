"""Tests for transport.signer.MessageSigner — Ed25519 sign/verify."""

from __future__ import annotations

import pytest

from turing.transport.signer import MessageSigner, SignatureError


def test_sign_then_verify_roundtrip_returns_payload() -> None:
    signer = MessageSigner.generate()
    payload = b"echo:hello"

    signed = signer.sign(payload)
    verified = signer.verify(signed, trusted_public_keys=[signer.public_key])

    assert verified == payload


def test_verify_rejects_tampered_payload() -> None:
    signer = MessageSigner.generate()
    signed = signer.sign(b"echo:hello")

    tampered = signed.with_payload(b"echo:goodbye")

    with pytest.raises(SignatureError):
        signer.verify(tampered, trusted_public_keys=[signer.public_key])


def test_verify_rejects_unknown_signer() -> None:
    alice = MessageSigner.generate()
    mallory = MessageSigner.generate()
    signed = mallory.sign(b"echo:hello")

    with pytest.raises(SignatureError):
        alice.verify(signed, trusted_public_keys=[alice.public_key])
