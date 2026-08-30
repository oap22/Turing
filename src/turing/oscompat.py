"""Windows/POSIX compatibility helpers for the runtime (issue #399).

Every helper consults ``sys.platform`` at call time (never at import time) so
tests can monkeypatch the platform and exercise both branches from any host.
POSIX behavior is the identity: each helper returns exactly what the call
sites used before this module existed.
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

# Documented value of ``subprocess.CREATE_NO_WINDOW``; the constant only
# exists in the stdlib on Windows, so tests running on POSIX with a
# monkeypatched ``sys.platform`` need the literal.
_CREATE_NO_WINDOW = 0x08000000


def is_windows() -> bool:
    """True when running on native Windows (not WSL, which is ``linux``)."""
    return sys.platform == "win32"


def subprocess_creation_flags() -> int:
    """``creationflags`` for subprocess spawns: no flashing console on Windows.

    On Windows every subprocess spawned from a GUI-launched (or pythonw)
    process pops a console window unless ``CREATE_NO_WINDOW`` is set. On
    POSIX ``creationflags`` is accepted by ``subprocess.Popen`` but ignored,
    so returning 0 keeps call sites unconditional.
    """
    if is_windows():
        return int(getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW))
    return 0


def ping_argv(host: str, count: int, timeout_seconds: int) -> list[str]:
    """Platform-correct ``ping`` argv.

    Windows ping counts with ``-n`` and takes ``-w`` in *milliseconds*;
    POSIX ping counts with ``-c`` and takes ``-W`` in seconds. The POSIX
    branch is byte-identical to the argv ``NetworkTool._ping`` always used.
    """
    if is_windows():
        return ["ping", "-n", str(count), "-w", str(timeout_seconds * 1000), host]
    return ["ping", "-c", str(count), "-W", str(timeout_seconds), host]


def windows_shell_argv(command: str) -> list[str]:
    """argv that runs ``command`` under PowerShell on Windows.

    ``asyncio.create_subprocess_shell`` on Windows hands the command to
    ``cmd.exe`` (via ``%COMSPEC%``); the shell tool instead runs PowerShell
    explicitly so operators get one predictable, modern shell. ``-NoProfile``
    keeps user profiles from mutating tool behavior, ``-NonInteractive``
    makes prompts fail fast instead of hanging the tool. This is a plain
    pipe-connected subprocess, not a ConPTY: interactive/TUI programs that
    need a real console will not behave (see docs/operator/windows.md).
    """
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop, handler: Callable[[], None]
) -> Literal["loop", "signal"]:
    """Register SIGINT/SIGTERM shutdown handlers portably.

    ``loop.add_signal_handler`` raises :class:`NotImplementedError` on
    Windows (the Proactor loop — the 3.8+ default — has no signal support),
    so ``python -m turing`` would die during init step 9. Fall back to
    :func:`signal.signal`; the sync handler trampolines back onto the loop
    with ``call_soon_threadsafe`` because Windows delivers signals on a
    separate thread. Returns which mechanism was used, for logging/tests.
    """
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handler)
        return "loop"
    except NotImplementedError:

        def _sync_handler(_signum: int, _frame: Any) -> None:
            # The registration outlives the loop (signal.signal is
            # process-global): a Ctrl+C after asyncio.run() returns must be
            # a no-op, not a RuntimeError("Event loop is closed") crash.
            if loop.is_closed():
                return
            # Suppress the race where the loop closes between the check
            # and the call.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(handler)

        # SIGTERM exists on Windows but is effectively never delivered;
        # registering it is harmless and keeps the set symmetric. SIGBREAK
        # (Ctrl+Break / console close) is Windows-only.
        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig_obj = getattr(signal, sig_name, None)
            if sig_obj is not None:
                signal.signal(sig_obj, _sync_handler)
        return "signal"
