"""Tests for turing.oscompat — Windows/POSIX runtime compatibility helpers.

Every helper reads ``sys.platform`` at call time, so both branches are
exercised here by monkeypatching the platform; no test needs a real Windows
host.
"""

from __future__ import annotations

import signal
import sys
from typing import Any

import pytest

from turing import oscompat


def _fake_platform(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)


class TestIsWindows:
    def test_win32_is_windows(self, monkeypatch: pytest.MonkeyPatch):
        _fake_platform(monkeypatch, "win32")
        assert oscompat.is_windows() is True

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_posix_is_not_windows(self, monkeypatch: pytest.MonkeyPatch, platform: str):
        _fake_platform(monkeypatch, platform)
        assert oscompat.is_windows() is False


class TestSubprocessCreationFlags:
    def test_posix_is_zero(self, monkeypatch: pytest.MonkeyPatch):
        _fake_platform(monkeypatch, "linux")
        assert oscompat.subprocess_creation_flags() == 0

    def test_windows_is_create_no_window(self, monkeypatch: pytest.MonkeyPatch):
        _fake_platform(monkeypatch, "win32")
        # 0x08000000 is the documented CREATE_NO_WINDOW value; the stdlib
        # constant only exists on a real Windows host.
        assert oscompat.subprocess_creation_flags() == 0x08000000


class TestPingArgv:
    def test_posix_argv_unchanged(self, monkeypatch: pytest.MonkeyPatch):
        _fake_platform(monkeypatch, "linux")
        assert oscompat.ping_argv("1.1.1.1", 4, 10) == [
            "ping", "-c", "4", "-W", "10", "1.1.1.1",
        ]  # fmt: skip

    def test_windows_argv_uses_n_and_milliseconds(self, monkeypatch: pytest.MonkeyPatch):
        _fake_platform(monkeypatch, "win32")
        assert oscompat.ping_argv("1.1.1.1", 4, 10) == [
            "ping", "-n", "4", "-w", "10000", "1.1.1.1",
        ]  # fmt: skip


class TestWindowsShellArgv:
    def test_powershell_invocation_shape(self):
        argv = oscompat.windows_shell_argv("Get-ChildItem C:\\")
        assert argv[0] == "powershell.exe"
        assert "-NoProfile" in argv
        assert "-NonInteractive" in argv
        # The command is a single trailing argv token after -Command, never
        # re-parsed by an outer shell.
        assert argv[-2:] == ["-Command", "Get-ChildItem C:\\"]


class _FakeLoop:
    """Stands in for an event loop; configurable add_signal_handler support."""

    def __init__(self, supports_signals: bool) -> None:
        self.supports_signals = supports_signals
        self.added: list[tuple[Any, Any]] = []
        self.soon: list[Any] = []

    def add_signal_handler(self, sig: Any, handler: Any) -> None:
        if not self.supports_signals:
            raise NotImplementedError
        self.added.append((sig, handler))

    def call_soon_threadsafe(self, cb: Any) -> None:
        self.soon.append(cb)


class TestInstallSignalHandlers:
    def test_posix_uses_loop_handlers(self):
        loop = _FakeLoop(supports_signals=True)

        def handler() -> None:  # pragma: no cover - never invoked here
            pass

        mechanism = oscompat.install_signal_handlers(loop, handler)  # type: ignore[arg-type]
        assert mechanism == "loop"
        assert [sig for sig, _ in loop.added] == [signal.SIGINT, signal.SIGTERM]
        assert all(h is handler for _, h in loop.added)

    def test_windows_falls_back_to_signal_signal(self, monkeypatch: pytest.MonkeyPatch):
        loop = _FakeLoop(supports_signals=False)
        registered: dict[Any, Any] = {}
        monkeypatch.setattr(
            oscompat.signal,
            "signal",
            lambda sig, h: registered.__setitem__(sig, h),
        )

        fired: list[bool] = []
        mechanism = oscompat.install_signal_handlers(loop, lambda: fired.append(True))  # type: ignore[arg-type]
        assert mechanism == "signal"
        # SIGINT and SIGTERM always exist; SIGBREAK only on real Windows.
        assert signal.SIGINT in registered
        assert signal.SIGTERM in registered

        # The sync handler trampolines onto the loop, then runs the handler.
        registered[signal.SIGINT](signal.SIGINT, None)
        assert len(loop.soon) == 1
        loop.soon[0]()
        assert fired == [True]
