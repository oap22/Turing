"""NATS-backed Bus implementation.

Thin wrapper that maps the Bus protocol onto `nats-py`. TLS and nkey auth are
wired from `TuringConfig` so the public flip is purely a config change. This
module is intentionally minimal — verification, replay protection, and message
typing all live above it in `SignedTransport`.

Live integration is exercised under the `integration` marker; unit tests use
`InMemoryBus` instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient

    from turing.transport.bus import BytesHandler


class NatsBus:
    def __init__(self, client: NatsClient) -> None:
        self._client = client

    @classmethod
    async def connect(
        cls,
        *,
        url: str,
        tls_enabled: bool,
        nkey_seed: str | None,
        lan_only: bool,
    ) -> NatsBus:
        if lan_only and not _is_lan_url(url):
            raise ValueError(
                f"nats_lan_only=True but url {url!r} is not a private LAN address; "
                "set nats_lan_only=False to opt into public NATS"
            )
        import nats  # local import keeps unit tests free of the dep

        kwargs: dict[str, object] = {"servers": [url]}
        if tls_enabled:
            kwargs["tls"] = True
        if nkey_seed is not None:
            kwargs["nkeys_seed_str"] = nkey_seed
        client = await nats.connect(**kwargs)  # type: ignore[arg-type]
        return cls(client)

    async def publish(self, subject: str, payload: bytes) -> None:
        await self._client.publish(subject, payload)

    async def subscribe(self, subject: str, handler: BytesHandler) -> None:
        async def _on_msg(msg: object) -> None:
            await handler(msg.data)  # type: ignore[attr-defined]

        await self._client.subscribe(subject, cb=_on_msg)

    async def close(self) -> None:
        await self._client.drain()


def _is_lan_url(url: str) -> bool:
    """Return True for loopback / private / .local URLs.

    Coarse check — full address parsing isn't needed; this is a defence-in-depth
    flag, not a security boundary.
    """
    prefixes = ("127.", "localhost", "10.", "192.168.")
    host = url.split("://", 1)[-1].split(":", 1)[0]
    if any(host.startswith(m) for m in prefixes):
        return True
    if host.endswith(".local"):
        return True
    if host.startswith("172."):
        try:
            second_octet = int(host.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second_octet <= 31
    return False
