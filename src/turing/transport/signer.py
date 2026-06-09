"""Ed25519 message signing for the Turing transport layer.

Every `MeshMessage` on the runtime bus is signed by the sender and verified by
the receiver against a set of trusted public keys. Replay protection lives in
``transport.envelope``; this module is purely about cryptographic identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


class SignatureError(Exception):
    """Raised when a signed message fails verification."""


@dataclass(frozen=True)
class SignedMessage:
    payload: bytes
    signature: bytes
    sender_public_key: bytes

    def with_payload(self, payload: bytes) -> SignedMessage:
        return replace(self, payload=payload)


class MessageSigner:
    """Sign and verify byte payloads with Ed25519."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self._public_key_bytes = private_key.public_key().public_bytes(
            encoding=Encoding.Raw, format=PublicFormat.Raw
        )

    @classmethod
    def generate(cls) -> MessageSigner:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_seed_hex(cls, seed_hex: str) -> MessageSigner:
        """Build a signer from a hex-encoded 32-byte Ed25519 private-key seed.

        This is the serialization the ``TURING_MESH_SIGNING_SEED`` config
        field uses (issue #348). Raises ``ValueError`` on malformed input.
        """
        try:
            seed = bytes.fromhex(seed_hex.strip())
        except ValueError as exc:
            raise ValueError("signing seed is not valid hex") from exc
        if len(seed) != 32:
            raise ValueError(f"signing seed must be 32 bytes, got {len(seed)}")
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @property
    def public_key(self) -> bytes:
        return self._public_key_bytes

    def sign(self, payload: bytes) -> SignedMessage:
        signature = self._private_key.sign(payload)
        return SignedMessage(
            payload=payload,
            signature=signature,
            sender_public_key=self._public_key_bytes,
        )

    def verify(
        self,
        signed: SignedMessage,
        *,
        trusted_public_keys: Iterable[bytes],
    ) -> bytes:
        trusted = {bytes(k) for k in trusted_public_keys}
        if signed.sender_public_key not in trusted:
            raise SignatureError("sender public key is not trusted")

        public_key = Ed25519PublicKey.from_public_bytes(signed.sender_public_key)
        try:
            public_key.verify(signed.signature, signed.payload)
        except InvalidSignature as exc:
            raise SignatureError("signature does not match payload") from exc
        return signed.payload
