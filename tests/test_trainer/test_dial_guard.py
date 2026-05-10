"""The trainer is a pull-only client and must never bind a listening socket."""

from __future__ import annotations

import socket

import pytest

from turing.learning.trainer.dial_guard import (
    InboundConnectionRefusedError,
    PullOnlySocketGuard,
)


class TestPullOnlySocketGuard:
    def test_blocks_socket_listen(self) -> None:
        guard = PullOnlySocketGuard()
        with guard:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", 0))
                with pytest.raises(InboundConnectionRefusedError):
                    s.listen(1)
            finally:
                s.close()

    def test_allows_outbound_connect(self) -> None:
        guard = PullOnlySocketGuard()
        with guard:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # connect to a closed port should fail with ConnectionRefused
            # (the OS error), not InboundConnectionRefusedError (our guard) —
            # i.e., the guard must not interfere with outbound traffic.
            with pytest.raises((ConnectionRefusedError, OSError)) as excinfo:
                s.connect(("127.0.0.1", 1))
            assert not isinstance(excinfo.value, InboundConnectionRefusedError)
            s.close()

    def test_restored_on_exit(self) -> None:
        original = socket.socket.listen
        with PullOnlySocketGuard():
            assert socket.socket.listen is not original
        assert socket.socket.listen is original
