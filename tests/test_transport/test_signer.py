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


def test_from_seed_hex_roundtrip() -> None:
    # Issue #348: TURING_MESH_SIGNING_SEED is a hex 32-byte Ed25519 seed.
    seed_hex = "11" * 32
    signer_a = MessageSigner.from_seed_hex(seed_hex)
    signer_b = MessageSigner.from_seed_hex(seed_hex)

    # Deterministic: same seed -> same identity.
    assert signer_a.public_key == signer_b.public_key

    signed = signer_a.sign(b"hello")
    assert signer_b.verify(signed, trusted_public_keys=[signer_b.public_key]) == b"hello"


def test_from_seed_hex_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="not valid hex"):
        MessageSigner.from_seed_hex("zz" * 32)
    with pytest.raises(ValueError, match="32 bytes"):
        MessageSigner.from_seed_hex("ab" * 16)
