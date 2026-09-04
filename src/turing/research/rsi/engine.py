"""Engines for the RSI workstation loop: the real ``claude -p`` CLI and a scripted fake.

An :class:`~turing.research.rsi.contracts.Engine` runs one round's prompt in
the sandbox and reports how it ended. The loop never inspects the engine's
stdout for a score — the verifier does the measuring (I2) — so an engine's
only obligations are: run in ``cwd``, respect the wall-clock cap, and report
``exit_code`` / ``timed_out`` truthfully.

What this module guarantees:

* :class:`ClaudeCliEngine` starts the CLI with
  :func:`asyncio.create_subprocess_exec` (argv, never a shell) in its own
  session and **always** SIGKILLs that process group when the CLI is done —
  on timeout *and* on a normal exit — so no in-group child of the CLI
  outlives the round and edits a pinned file between the loop's lock checks
  (I1). A CLI that cannot be started is reported as ``exit_code=127`` with
  the OS error in ``stderr``, not raised, so the loop's consecutive-failure
  counter sees it like the bash script did.
* **The wall-clock cap is hard.** :func:`run_capped` waits for the process
  itself, not for its pipes: after the cap (or the exit) the group is killed
  and the pipe drain is bounded by :data:`DRAIN_GRACE_SECONDS`; a stray
  holder of the stdout/stderr pipe (a ``setsid``/daemonised descendant that
  survived the group kill) cannot stall the loop past that grace, the pipes
  are closed and whatever was captured is returned. The verifier runner
  shares this helper.
* :class:`FakeEngine` replays a script in order and records every call. A
  script item may be a ready :class:`EngineResult` or a callable that
  receives ``(prompt, cwd)`` and may mutate the sandbox — which is how tests
  stage tampering, metrics lines, and scaffold edits. Running past the end
  of the script is a :class:`~turing.research.contracts.ContractViolationError`
  unless a ``default`` result was given.

What it does not do:

* It does not sandbox anything. ``--permission-mode bypassPermissions`` is
  scoped to the sandbox directory by convention only (ADR 0011 §8); the
  cheat detector, not the engine, notices an escape.
* It cannot reach a descendant that left the process group (``setsid``, a
  double-fork daemon). Such a process is outside the boundary this package
  can enforce; the loop's defence in depth is to re-hash pinned files right
  before and right after the verifier runs, which still leaves a
  modify-and-restore race inside the verifier's own window to the OS-level
  boundary of ADR 0011 §8.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from turing.research.contracts import ContractViolationError
from turing.research.rsi.contracts import EngineResult

logger = structlog.get_logger(__name__)

__all__ = [
    "DRAIN_GRACE_SECONDS",
    "CappedOutput",
    "ClaudeCliEngine",
    "FakeCall",
    "FakeEngine",
    "ScriptItem",
    "kill_process_group",
    "ok_result",
    "run_capped",
]

#: What a :class:`FakeEngine` script item may be.
ScriptItem = (
    EngineResult
    | Callable[[str, Path], EngineResult]
    | Callable[[str, Path], Awaitable[EngineResult]]
)

#: Exit code reported when the CLI binary cannot be started (shell convention).
_EXIT_NOT_FOUND = 127
#: Exit code reported for a killed, timed-out CLI (``timeout(1)`` convention).
_EXIT_TIMED_OUT = 124
#: How long the pipe drain (and the post-kill wait) may take once the process is done.
DRAIN_GRACE_SECONDS: float = 2.0
_PUMP_CHUNK = 1 << 16
#: How often the exit poll looks at ``proc.returncode``.
_EXIT_POLL_SECONDS: float = 0.05


@dataclass(frozen=True, slots=True)
class CappedOutput:
    """What :func:`run_capped` captured: the exit code and the pipe bytes, plus how it ended."""

    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    #: ``True`` when a pipe was still held open after the grace and had to be closed.
    pipe_abandoned: bool


def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the process group ``proc`` leads (``start_new_session=True``); fall back to it alone.

    Called after a normal exit too: the group id equals the leader's pid, and
    the group outlives a leader that exited while its children run on.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            if proc.returncode is None:
                proc.kill()


async def _pump(stream: asyncio.StreamReader, into: bytearray) -> None:
    while True:
        chunk = await stream.read(_PUMP_CHUNK)
        if not chunk:
            return
        into += chunk


async def _wait_exited(proc: asyncio.subprocess.Process, timeout_seconds: float) -> bool:
    """``True`` once the process itself has exited, ``False`` at the deadline.

    Polls ``proc.returncode`` rather than awaiting ``proc.wait()``: on
    Python 3.11 ``wait()`` resolves only after every pipe has closed, so a
    stray holder of stdout would stall it past the cap — the very thing this
    module guards against. The return code is set by the child watcher as
    soon as the process is reaped, pipes or not.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while proc.returncode is None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_EXIT_POLL_SECONDS, remaining))
    return True


def _close_pipes(proc: asyncio.subprocess.Process) -> None:
    """Close the stdout/stderr pipe transports so a stray holder no longer matters."""
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    for fd in (1, 2):
        try:
            pipe = transport.get_pipe_transport(fd)
        except Exception:
            pipe = None
        if pipe is not None:
            with contextlib.suppress(Exception):
                pipe.close()


async def run_capped(
    proc: asyncio.subprocess.Process,
    *,
    timeout_seconds: float,
    grace_seconds: float = DRAIN_GRACE_SECONDS,
) -> CappedOutput:
    """Wait for ``proc`` (started with pipes and ``start_new_session=True``) under a hard cap.

    The cap applies to the process, not its pipes. Whatever happens the
    group is SIGKILLed once the process is done, and reading the pipes to
    EOF is given at most ``grace_seconds``; then the pipes are closed and
    the bytes read so far are returned.
    """
    if proc.stdout is None or proc.stderr is None:
        raise ContractViolationError("run_capped needs a process started with stdout/stderr pipes")
    out, err = bytearray(), bytearray()
    readers = [
        asyncio.ensure_future(_pump(proc.stdout, out)),
        asyncio.ensure_future(_pump(proc.stderr, err)),
    ]
    try:
        timed_out = not await _wait_exited(proc, timeout_seconds)
    except asyncio.CancelledError:
        # Ctrl-C / task cancellation: never leave the detached group running.
        kill_process_group(proc)
        for task in readers:
            task.cancel()
        _close_pipes(proc)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(*readers, return_exceptions=True)
        logger.warning("rsi.process.cancelled", pid=proc.pid)
        raise
    kill_process_group(proc)
    if proc.returncode is None:
        await _wait_exited(proc, grace_seconds)
    _done, pending = await asyncio.wait(readers, timeout=grace_seconds)
    abandoned = bool(pending)
    if abandoned:
        for task in pending:
            task.cancel()
        _close_pipes(proc)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(*pending, return_exceptions=True)
        logger.warning(
            "rsi.process.pipe_abandoned",
            pid=proc.pid,
            grace_seconds=grace_seconds,
            hint="a descendant outside the process group still held stdout/stderr",
        )
    if proc.returncode is None:
        # Unreapable within the grace (should not happen after SIGKILL); do not block on it.
        _close_pipes(proc)
        with contextlib.suppress(Exception):
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()
        exit_code = -1
    else:
        exit_code = proc.returncode
    return CappedOutput(
        exit_code=exit_code,
        stdout=bytes(out),
        stderr=bytes(err),
        timed_out=timed_out,
        pipe_abandoned=abandoned,
    )


class ClaudeCliEngine:
    """Runs ``claude -p <prompt> --permission-mode bypassPermissions --output-format text``."""

    def __init__(self, claude_bin: str = "claude") -> None:
        if not claude_bin:
            raise ContractViolationError("claude_bin must be a non-empty command name")
        self._bin = claude_bin

    def argv(self, prompt: str) -> list[str]:
        return [
            self._bin,
            "-p",
            prompt,
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "text",
        ]

    async def run(self, prompt: str, *, cwd: Path, timeout_seconds: float) -> EngineResult:
        if timeout_seconds <= 0:
            raise ContractViolationError("timeout_seconds must be positive")
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv(prompt),
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            logger.error("rsi.engine.start_failed", command=self._bin, error=str(exc))
            return EngineResult(
                exit_code=_EXIT_NOT_FOUND,
                stdout="",
                stderr=f"could not start {self._bin!r}: {exc}",
                wall_seconds=time.monotonic() - started,
                timed_out=False,
            )
        capped = await run_capped(proc, timeout_seconds=timeout_seconds)
        wall = time.monotonic() - started
        if capped.timed_out:
            logger.warning("rsi.engine.timed_out", wall_seconds=wall, timeout=timeout_seconds)
            return EngineResult(
                exit_code=_EXIT_TIMED_OUT,
                stdout=capped.stdout.decode("utf-8", "replace"),
                stderr=capped.stderr.decode("utf-8", "replace"),
                wall_seconds=wall,
                timed_out=True,
            )
        logger.info("rsi.engine.finished", exit_code=capped.exit_code, wall_seconds=round(wall, 3))
        return EngineResult(
            exit_code=capped.exit_code,
            stdout=capped.stdout.decode("utf-8", "replace"),
            stderr=capped.stderr.decode("utf-8", "replace"),
            wall_seconds=wall,
            timed_out=False,
        )


@dataclass(frozen=True, slots=True)
class FakeCall:
    """One recorded :meth:`FakeEngine.run` invocation."""

    prompt: str
    cwd: Path
    timeout_seconds: float


@dataclass(slots=True)
class FakeEngine:
    """Replays ``script`` in order; each item is a result or a callable producing one.

    Callables get ``(prompt, cwd)`` and may be sync or async. ``calls`` keeps
    every invocation so a test can assert on the prompt the loop built.
    """

    script: Sequence[ScriptItem] = ()
    default: EngineResult | None = None
    calls: list[FakeCall] = field(default_factory=list)
    _cursor: int = 0

    async def run(self, prompt: str, *, cwd: Path, timeout_seconds: float) -> EngineResult:
        self.calls.append(FakeCall(prompt=prompt, cwd=cwd, timeout_seconds=timeout_seconds))
        if self._cursor >= len(self.script):
            if self.default is None:
                raise ContractViolationError(
                    f"FakeEngine script exhausted after {len(self.script)} call(s) and no default"
                )
            return self.default
        item = self.script[self._cursor]
        self._cursor += 1
        if isinstance(item, EngineResult):
            return item
        outcome = item(prompt, cwd)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if not isinstance(outcome, EngineResult):
            raise ContractViolationError("FakeEngine script callables must return an EngineResult")
        return outcome

    @property
    def remaining(self) -> int:
        return max(0, len(self.script) - self._cursor)


def ok_result(stdout: str = "", *, wall_seconds: float = 1.0) -> EngineResult:
    """A successful :class:`EngineResult`; convenience for scripts and tests."""
    return EngineResult(
        exit_code=0, stdout=stdout, stderr="", wall_seconds=wall_seconds, timed_out=False
    )
