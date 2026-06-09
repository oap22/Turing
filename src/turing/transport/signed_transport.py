"""Signed transport — wraps a Bus with sender auth and replay protection.

Outbound: serialise a `MeshMessage`, sign it, prepend the signer's public key,
publish to the wrapped bus. Inbound: split sender key + signature + payload,
verify the signature against `trusted_keys`, check the verifying key is the
one bound to the envelope's claimed `sender_id`, run the payload through the
`ReplayWindow`, only then deliver to the subscriber callback.

`trusted_keys` is a mapping of node_id -> Ed25519 public key bytes. The
binding check is what stops a holder of one trusted key from impersonating
any other trusted node by writing an arbitrary `sender_id` into the envelope
(issue #347) — membership in the trusted set alone is not identity.

A handler that raises is reported via `on_error` so a single bad message can't
silently kill the subscription.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from turing.transport.envelope import MeshMessage, ReplayError, ReplayWindow
from turing.transport.signer import MessageSigner, SignatureError, SignedMessage

if TYPE_CHECKING:
    from turing.transport.bus import Bus

MessageHandler = Callable[[MeshMessage], Awaitable[None] | None]
ErrorHandler = Callable[[Exception], None]


class UntrustedSenderError(Exception):
    """Raised when an inbound message's sender key is not in trusted_keys."""


class SenderBindingError(UntrustedSenderError):
    """Raised when a trusted key signs an envelope claiming someone else's sender_id.

    The signature itself is valid and the key is trusted — but the key is not
    the one provisioned for the node named in the envelope's `sender_id`, so
    accepting the message would let one trusted node impersonate another.
    """


class SignedTransport:
    def __init__(
        self,
        *,
        bus: Bus,
        signer: MessageSigner,
        trusted_keys: Mapping[str, bytes],
        now_ms: Callable[[], int],
        replay_ttl_ms: int = 60_000,
    ) -> None:
        if not isinstance(trusted_keys, Mapping):
            raise TypeError(
                "trusted_keys must be a mapping of node_id -> Ed25519 public key "
                "bytes. Flat key collections are rejected: they cannot bind a "
                "verified key to the claimed sender_id, which lets any trusted "
                "key impersonate any other trusted node (issue #347). Migrate "
                "to {node_id: public_key_bytes}."
            )
        self._bus = bus
        self._signer = signer
        self._trusted: dict[str, bytes] = {
            node_id: bytes(key) for node_id, key in trusted_keys.items()
        }
        self._replay = ReplayWindow(ttl_ms=replay_ttl_ms, now_ms=now_ms)

    async def publish(self, message: MeshMessage) -> None:
        signed = self._signer.sign(message.to_bytes())
        await self._bus.publish(message.subject, _encode(signed))

    async def subscribe(
        self,
        subject: str,
        handler: MessageHandler,
        *,
        on_error: ErrorHandler | None = None,
    ) -> None:
        async def _on_bytes(raw: bytes) -> None:
            try:
                signed = _decode(raw)
                payload = self._signer.verify(signed, trusted_public_keys=self._trusted.values())
                message = MeshMessage.from_bytes(payload)
                # Identity binding: the verifying key must be the exact key
                # provisioned for the claimed sender_id, not merely a member
                # of the trusted set.
                if self._trusted.get(message.sender_id) != signed.sender_public_key:
                    raise SenderBindingError(
                        f"verifying key is not bound to claimed sender_id {message.sender_id!r}"
                    )
                self._replay.observe(
                    request_id=message.request_id, timestamp_ms=message.timestamp_ms
                )
            except (SignatureError, UntrustedSenderError, ReplayError, ValueError) as exc:
                if on_error is not None:
                    on_error(exc)
                return

            result = handler(message)
            if result is not None:
                await result

        await self._bus.subscribe(subject, _on_bytes)


def _encode(signed: SignedMessage) -> bytes:
    frame = {
        "k": signed.sender_public_key.hex(),
        "s": signed.signature.hex(),
        "p": signed.payload.hex(),
    }
    return json.dumps(frame, separators=(",", ":")).encode("utf-8")


def _decode(raw: bytes) -> SignedMessage:
    frame = json.loads(raw.decode("utf-8"))
    return SignedMessage(
        sender_public_key=bytes.fromhex(frame["k"]),
        signature=bytes.fromhex(frame["s"]),
        payload=bytes.fromhex(frame["p"]),
    )
