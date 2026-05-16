"""Signed transport — wraps a Bus with sender auth and replay protection.

Outbound: serialise a `MeshMessage`, sign it, prepend the signer's public key,
publish to the wrapped bus. Inbound: split sender key + signature + payload,
verify the signature against `trusted_keys`, run the payload through the
`ReplayWindow`, only then deliver to the subscriber callback.

A handler that raises is reported via `on_error` so a single bad message can't
silently kill the subscription.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from turing.transport.envelope import MeshMessage, ReplayError, ReplayWindow
from turing.transport.signer import MessageSigner, SignatureError, SignedMessage

if TYPE_CHECKING:
    from turing.transport.bus import Bus

MessageHandler = Callable[[MeshMessage], Awaitable[None] | None]
ErrorHandler = Callable[[Exception], None]


class UntrustedSenderError(Exception):
    """Raised when an inbound message's sender key is not in trusted_keys."""


class SignedTransport:
    def __init__(
        self,
        *,
        bus: Bus,
        signer: MessageSigner,
        trusted_keys: list[bytes],
        now_ms: Callable[[], int],
        replay_ttl_ms: int = 60_000,
    ) -> None:
        self._bus = bus
        self._signer = signer
        self._trusted = [bytes(k) for k in trusted_keys]
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
                payload = self._signer.verify(signed, trusted_public_keys=self._trusted)
                message = MeshMessage.from_bytes(payload)
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
