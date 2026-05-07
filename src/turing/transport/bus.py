"""Bus protocol and an in-memory fake used by tests.

The real implementation in `transport.nats_bus` wraps `nats-py`; the in-memory
bus here lets unit tests exercise `SignedTransport` end-to-end without a live
broker.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Protocol

BytesHandler = Callable[[bytes], Awaitable[None]]


class Bus(Protocol):
    async def publish(self, subject: str, payload: bytes) -> None: ...
    async def subscribe(self, subject: str, handler: BytesHandler) -> None: ...


class InMemoryBus:
    """In-process bus that delivers published bytes to all subject subscribers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[BytesHandler]] = defaultdict(list)

    async def publish(self, subject: str, payload: bytes) -> None:
        for handler in list(self._handlers.get(subject, ())):
            await handler(payload)

    async def subscribe(self, subject: str, handler: BytesHandler) -> None:
        self._handlers[subject].append(handler)
