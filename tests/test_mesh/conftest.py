"""Fake Pyre fixture for mesh tests — no real UDP traffic.

Mimics the subset of the ``pyre.Pyre`` interface that ``turing.mesh.discovery``
relies on. Tests can inject events into ``incoming_events`` and inspect the
``shouts``, ``whispers``, ``joins``, ``headers``, and lifecycle calls made by
production code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeSocket:
    """Stand-in for the zmq socket returned by ``Pyre.socket()``.

    ``poll`` returns whatever the owning :class:`FakePyre` has queued so the
    discovery loop can be driven deterministically.
    """

    pyre: FakePyre

    def poll(self, timeout: int = 0) -> int:
        # Mirror zmq.Socket.poll's int return — non-zero means "has events".
        return 1 if self.pyre.incoming_events else 0


@dataclass
class FakePyre:
    """In-memory stand-in for ``pyre.Pyre``.

    Records every interaction so tests can assert on protocol behaviour
    without touching the network.
    """

    name: str
    headers: dict[str, str] = field(default_factory=dict)
    joins: list[str] = field(default_factory=list)
    leaves: list[str] = field(default_factory=list)
    shouts: list[tuple[str, bytes]] = field(default_factory=list)
    whispers: list[tuple[str, bytes]] = field(default_factory=list)
    incoming_events: list[list[Any]] = field(default_factory=list)
    started: bool = False
    stopped: bool = False
    raise_on_start: Exception | None = None
    raise_on_stop: Exception | None = None
    raise_on_recv: Exception | None = None

    # ----- header/membership API -----
    def set_header(self, key: str, value: str) -> None:
        self.headers[key] = value

    def join(self, group: str) -> None:
        self.joins.append(group)

    def leave(self, group: str) -> None:
        self.leaves.append(group)

    def start(self) -> None:
        if self.raise_on_start is not None:
            raise self.raise_on_start
        self.started = True

    def stop(self) -> None:
        if self.raise_on_stop is not None:
            raise self.raise_on_stop
        self.stopped = True

    # ----- message API -----
    def shout(self, group: str, data: bytes) -> None:
        self.shouts.append((group, data))

    def whisper(self, peer: str, data: bytes) -> None:
        self.whispers.append((peer, data))

    # ----- recv API -----
    def socket(self) -> FakeSocket:
        return FakeSocket(pyre=self)

    def recv(self) -> list[Any] | None:
        if self.raise_on_recv is not None:
            raise self.raise_on_recv
        if not self.incoming_events:
            return None
        return self.incoming_events.pop(0)

    # ----- test helpers -----
    def inject_enter(
        self,
        peer_uuid: str,
        peer_name: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Queue an ENTER event with optional headers."""
        self.incoming_events.append(
            [b"ENTER", peer_uuid.encode(), peer_name.encode(), headers or {}]
        )

    def inject_exit(self, peer_uuid: str, peer_name: str) -> None:
        self.incoming_events.append([b"EXIT", peer_uuid.encode(), peer_name.encode()])

    def inject_shout(self, peer_uuid: str, peer_name: str, payload: bytes) -> None:
        self.incoming_events.append([b"SHOUT", peer_uuid.encode(), peer_name.encode(), {}, payload])

    def inject_whisper(self, peer_uuid: str, peer_name: str, payload: bytes) -> None:
        self.incoming_events.append(
            [b"WHISPER", peer_uuid.encode(), peer_name.encode(), {}, payload]
        )
