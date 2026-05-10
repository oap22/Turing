"""PullOnlySocketGuard — refuses any inbound connection.

The trainer is a pull-only client; it must never accept inbound traffic. This
guard installs a context-bound monkey-patch on ``socket.socket.listen`` so any
attempt to start a TCP listener raises ``InboundConnectionRefusedError`` instead.

This is defense-in-depth, not a substitute for firewall rules. The systemd
unit also sets ``RestrictAddressFamilies`` and ``PrivateNetwork`` where
possible; the guard catches the case where those are misconfigured.
"""

from __future__ import annotations

import socket
from typing import Any


class InboundConnectionRefusedError(RuntimeError):
    """Raised when something tries to bind a listening socket inside a guard."""


class PullOnlySocketGuard:
    """Context manager that blocks ``socket.listen`` for the duration."""

    def __init__(self) -> None:
        self._original: Any | None = None

    def __enter__(self) -> PullOnlySocketGuard:
        self._original = socket.socket.listen

        def _refuse(self_socket: socket.socket, *args: Any, **kwargs: Any) -> None:
            raise InboundConnectionRefusedError(
                "trainer is a pull-only client; listening sockets are forbidden"
            )

        socket.socket.listen = _refuse  # type: ignore[method-assign,assignment]
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._original is not None:
            socket.socket.listen = self._original  # type: ignore[method-assign]
            self._original = None
