"""Engines for the RSI workstation loop: Claude, Codex, and a scripted fake.

An :class:`~turing.research.rsi.contracts.Engine` runs one round's prompt in
the sandbox and reports how it ended. The loop never inspects the engine's
stdout for a score — the verifier does the measuring (I2) — so an engine's
only obligations are: run in ``cwd``, respect the wall-clock cap, and report
``exit_code`` / ``timed_out`` truthfully.

What this module guarantees:

* :class:`ClaudeCliEngine` and :class:`CodexCliEngine` start their CLIs with
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
* **Output is bounded.** :func:`run_capped` retains at most
  :data:`DEFAULT_MAX_OUTPUT_BYTES` per stream by default. A first discarded
  byte signals overflow and causes :data:`OUTPUT_LIMIT_EXIT`; callers must
  treat :attr:`CappedOutput.output_limit_exceeded` as authoritative even if
  the leader races to exit zero.
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
import errno
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
    "CODEX_MODEL",
    "CODEX_REASONING_EFFORT",
    "CODEX_SANDBOX",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DRAIN_GRACE_SECONDS",
    "OUTPUT_LIMIT_EXIT",
    "PROMPT_MAX_BYTES",
    "PROMPT_TOO_LARGE_EXIT",
    "CappedOutput",
    "ClaudeCliEngine",
    "CodexCliEngine",
    "FakeCall",
    "FakeEngine",
    "ScriptItem",
    "kill_process_group",
    "ok_result",
    "prompt_too_large",
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
#: Exit code reported when either captured output stream exceeds its byte cap.
OUTPUT_LIMIT_EXIT: int = 125
#: Exit code reported when the prompt is too large to pass as one argv string.
PROMPT_TOO_LARGE_EXIT: int = 126
#: Largest prompt (UTF-8 bytes) an engine will pass on argv. Both CLIs take
#: the prompt as a single argument, and Linux caps one argument at 128 KiB
#: (``MAX_ARG_STRLEN``); ``execve`` then fails with ``E2BIG`` before the CLI
#: runs. The cap sits under that limit so the failure is reported as what it
#: is — a prompt that outgrew argv, almost always a bloated ``SCAFFOLD.md`` —
#: instead of as a CLI that could not be started.
PROMPT_MAX_BYTES: int = 120 * 1024
#: Default retained output per stream. The cap is deliberately per stream.
DEFAULT_MAX_OUTPUT_BYTES: int = 8 * 1024 * 1024
#: How long the pipe drain (and the post-kill wait) may take once the process is done.
DRAIN_GRACE_SECONDS: float = 2.0
_PUMP_CHUNK = 1 << 16
#: How often the exit poll looks at ``proc.returncode``.
_EXIT_POLL_SECONDS: float = 0.05


@dataclass(frozen=True, slots=True)
class CappedOutput:
    """What :func:`run_capped` captured and how the process ended.

    ``stdout`` and ``stderr`` contain at most the requested number of bytes
    each. ``output_limit_exceeded`` is authoritative when deciding whether
    the exit was successful: a process that writes byte ``limit + 1`` is an
    overflow even if it races to return zero.
    """

    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    #: ``True`` when a pipe was still held open after the grace and had to be closed.
    pipe_abandoned: bool
    #: ``True`` when stdout or stderr produced more than the configured cap.
    output_limit_exceeded: bool = False


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


async def _pump(
    stream: asyncio.StreamReader,
    into: bytearray,
    *,
    max_output_bytes: int,
    overflow: asyncio.Event,
    overflow_at: list[float],
) -> None:
    """Copy a pipe into a bounded buffer and signal on the first discarded byte.

    ``StreamReader.read`` may return a chunk much larger than the remaining
    capacity. Slice before mutating ``into`` so the actual capture buffer's
    high-water length never exceeds the cap; discarded bytes are never kept.
    The pump returns as soon as overflow is observed so the supervisor can
    kill a still-running producer promptly.
    """
    while True:
        chunk = await stream.read(_PUMP_CHUNK)
        if not chunk:
            return
        remaining = max_output_bytes - len(into)
        if len(chunk) > remaining:
            if remaining:
                into.extend(chunk[:remaining])
            if not overflow.is_set():
                overflow_at.append(asyncio.get_running_loop().time())
            overflow.set()
            return
        into.extend(chunk)


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


async def _join_tasks(
    tasks: Sequence[asyncio.Task[object]], *, cancel_pending: bool = False
) -> list[object]:
    """Join tasks and retrieve every exception, including pump failures."""
    if cancel_pending:
        for task in tasks:
            if not task.done():
                task.cancel()
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            logger.warning("rsi.process.reader_failed", task=repr(task), error=repr(result))
    return results


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
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> CappedOutput:
    """Wait for ``proc`` under hard time and per-stream output caps.

    ``proc`` must have stdout and stderr pipes and should have been started
    with ``start_new_session=True``. ``max_output_bytes`` is a positive int
    (``bool`` is rejected) and applies independently to each stream. The
    default retains at most 8 MiB per stream. The helper owns the process
    group and its pipes: invalid arguments still kill that group and close
    its pipes before raising ``ContractViolationError``.

    Whatever happens, the group is SIGKILLed once the process exits, the
    wall deadline expires, or either pump detects overflow. Pipe cleanup is
    bounded by ``grace_seconds``; then the pipes are closed and bytes read so
    far are returned. Overflow has result-code precedence over timeout.
    """
    if not isinstance(max_output_bytes, int) or isinstance(max_output_bytes, bool):
        await _cleanup_invalid_process(proc, grace_seconds)
        raise ContractViolationError(
            "max_output_bytes must be a positive integer (bool is invalid)"
        )
    if max_output_bytes <= 0:
        await _cleanup_invalid_process(proc, grace_seconds)
        raise ContractViolationError("max_output_bytes must be a positive integer")
    if proc.stdout is None or proc.stderr is None:
        await _cleanup_invalid_process(proc, grace_seconds)
        raise ContractViolationError("run_capped needs a process started with stdout/stderr pipes")

    out, err = bytearray(), bytearray()
    overflow = asyncio.Event()
    overflow_at: list[float] = []
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    readers: list[asyncio.Task[object]] = [
        asyncio.ensure_future(
            _pump(
                proc.stdout,
                out,
                max_output_bytes=max_output_bytes,
                overflow=overflow,
                overflow_at=overflow_at,
            )
        ),
        asyncio.ensure_future(
            _pump(
                proc.stderr,
                err,
                max_output_bytes=max_output_bytes,
                overflow=overflow,
                overflow_at=overflow_at,
            )
        ),
    ]
    exit_wait = asyncio.ensure_future(_wait_exited(proc, timeout_seconds))
    overflow_wait = asyncio.ensure_future(overflow.wait())
    waiters: list[asyncio.Task[object]] = [exit_wait, overflow_wait]
    try:
        done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if exit_wait in done:
            timed_out = not bool(exit_wait.result())
            output_limit_exceeded = overflow.is_set()
        else:
            # Overflow was detected before the process deadline. If the
            # deadline task happened to complete in the same loop turn, use
            # its actual result to preserve deterministic timeout semantics.
            output_limit_exceeded = overflow.is_set()
            timed_out = bool(exit_wait.done() and not exit_wait.result())
        # The process wait and overflow reader can complete in the same event
        # loop turn. If the pump crossed the cap before the deadline, that
        # overflow is the independent terminating condition; if it crossed
        # during post-deadline cleanup, retain the timeout flag as well.
        if timed_out and overflow_at and overflow_at[0] < deadline:
            timed_out = False
        await _join_tasks(waiters, cancel_pending=True)
        kill_process_group(proc)
        if proc.returncode is None:
            await _wait_exited(proc, grace_seconds)

        _done, pending = await asyncio.wait(readers, timeout=grace_seconds)
        abandoned = bool(pending)
        if abandoned:
            for task in pending:
                task.cancel()
            _close_pipes(proc)
            await _join_tasks(list(pending), cancel_pending=False)
            logger.warning(
                "rsi.process.pipe_abandoned",
                pid=proc.pid,
                grace_seconds=grace_seconds,
                hint="a descendant outside the process group still held stdout/stderr",
            )
        await _join_tasks(readers, cancel_pending=False)
        # A short-lived process can exit before the pump consumes its final
        # bytes. Detect overflow during this bounded drain as well.
        output_limit_exceeded = output_limit_exceeded or overflow.is_set()
        _close_pipes(proc)

        if proc.returncode is None:
            # Unreapable within the grace (should not happen after SIGKILL); do not block on it.
            with contextlib.suppress(Exception):
                transport = getattr(proc, "_transport", None)
                if transport is not None:
                    transport.close()
            exit_code = -1
        else:
            exit_code = proc.returncode
        if output_limit_exceeded:
            exit_code = OUTPUT_LIMIT_EXIT
        return CappedOutput(
            exit_code=exit_code,
            stdout=bytes(out),
            stderr=bytes(err),
            timed_out=timed_out,
            pipe_abandoned=abandoned,
            output_limit_exceeded=output_limit_exceeded,
        )
    except asyncio.CancelledError:
        # Cancellation can arrive while waiting for exit, during the kill
        # grace, or while draining an escaped descendant's pipe. Every path
        # owns the group and reader tasks, so clean all of them before the
        # caller observes the original CancelledError.
        kill_process_group(proc)
        _close_pipes(proc)
        await _join_tasks(waiters + readers, cancel_pending=True)
        with contextlib.suppress(Exception):
            await _wait_exited(proc, max(0.0, grace_seconds))
        _close_pipes(proc)
        logger.warning("rsi.process.cancelled", pid=proc.pid)
        raise


async def _cleanup_invalid_process(proc: asyncio.subprocess.Process, grace_seconds: float) -> None:
    """Issue ownership cleanup and reap before an invalid-limit raise.

    Argument validation runs before any capture allocation. The process has
    already been spawned, however, so still kill its group and close the
    parent-side pipe transports. Waiting for the leader is bounded by the
    existing grace; this prevents an invalid call from stranding a child while
    preserving the validation-before-buffer-allocation guarantee.
    """
    kill_process_group(proc)
    _close_pipes(proc)
    with contextlib.suppress(Exception):
        await _wait_exited(proc, max(0.0, grace_seconds))
    _close_pipes(proc)


def prompt_too_large(prompt: str, *, command: str, started: float) -> EngineResult | None:
    """The engine failure for a prompt over :data:`PROMPT_MAX_BYTES`, else ``None``.

    Checked before ``execve`` so the round is recorded as an engine failure
    with a diagnosis the operator can act on. The byte count is in the
    message; the loop's own accounting records the prompt size on the round.
    """
    size = len(prompt.encode("utf-8"))
    if size <= PROMPT_MAX_BYTES:
        return None
    logger.error(
        "rsi.engine.prompt_too_large", command=command, bytes=size, max_bytes=PROMPT_MAX_BYTES
    )
    return EngineResult(
        exit_code=PROMPT_TOO_LARGE_EXIT,
        stdout="",
        stderr=(
            f"prompt is {size} bytes, over the {PROMPT_MAX_BYTES}-byte argv cap; "
            f"{command!r} was not started. SCAFFOLD.md is prepended to every round "
            "prompt: shrink it (or reject the self-edit that grew it) and resume"
        ),
        wall_seconds=time.monotonic() - started,
        timed_out=False,
    )


def _start_failed(exc: OSError, *, command: str, started: float) -> EngineResult:
    """Map an ``execve`` failure to an engine result, naming ``E2BIG`` for what it is."""
    logger.error("rsi.engine.start_failed", command=command, error=str(exc))
    if exc.errno == errno.E2BIG:
        return EngineResult(
            exit_code=PROMPT_TOO_LARGE_EXIT,
            stdout="",
            stderr=(
                f"could not start {command!r}: the argument list is too long for this OS "
                f"({exc.strerror}); the prompt (SCAFFOLD.md included) must shrink"
            ),
            wall_seconds=time.monotonic() - started,
            timed_out=False,
        )
    return EngineResult(
        exit_code=_EXIT_NOT_FOUND,
        stdout="",
        stderr=f"could not start {command!r}: {exc}",
        wall_seconds=time.monotonic() - started,
        timed_out=False,
    )


class ClaudeCliEngine:
    """Runs ``claude -p <prompt> --permission-mode bypassPermissions --output-format text``.

    A prompt over :data:`PROMPT_MAX_BYTES` is refused with
    :data:`PROMPT_TOO_LARGE_EXIT` before the CLI is started.
    """

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
        refused = prompt_too_large(prompt, command=self._bin, started=started)
        if refused is not None:
            return refused
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
            return _start_failed(exc, command=self._bin, started=started)
        capped = await run_capped(proc, timeout_seconds=timeout_seconds)
        wall = time.monotonic() - started
        if capped.output_limit_exceeded:
            diagnostic = (
                "[rsi output limit exceeded; stdout/stderr capture was capped at "
                f"{DEFAULT_MAX_OUTPUT_BYTES} bytes per stream]"
            )
            stderr = capped.stderr.decode("utf-8", "replace")
            stderr = f"{stderr}\n{diagnostic}" if stderr else diagnostic
            logger.warning(
                "rsi.engine.output_limit_exceeded",
                wall_seconds=wall,
                max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
            )
            return EngineResult(
                exit_code=OUTPUT_LIMIT_EXIT,
                stdout=capped.stdout.decode("utf-8", "replace"),
                stderr=stderr,
                wall_seconds=wall,
                timed_out=False,
            )
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


# Codex's engine identity is deliberately fixed for the RSI campaign. A
# caller may choose Claude, Codex, or the test-only fake, but cannot silently
# change the model or reasoning budget under the same CLI engine name.
CODEX_MODEL: str = "gpt-5.6-luna"
CODEX_REASONING_EFFORT: str = "xhigh"
CODEX_SANDBOX: str = "workspace-write"


class CodexCliEngine:
    """Run one round with the fixed bounded Codex Luna engine.

    The process is launched as ``codex exec`` in the supplied sandbox. The
    explicit model, reasoning effort, and workspace-write sandbox are part of
    the argv contract so a missing or unavailable Codex installation fails as
    an engine failure; it never falls back to Claude or another model.
    """

    def __init__(self, codex_bin: str = "codex", *, add_dirs: Sequence[Path] = ()) -> None:
        if not codex_bin:
            raise ContractViolationError("codex_bin must be a non-empty command name")
        self._bin = codex_bin
        self._add_dirs = tuple(Path(directory) for directory in add_dirs)

    def argv(self, prompt: str) -> list[str]:
        argv = [
            self._bin,
            "exec",
            "-m",
            CODEX_MODEL,
            "-c",
            f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"',
            "--sandbox",
            CODEX_SANDBOX,
            "--color",
            "never",
        ]
        for directory in self._add_dirs:
            argv.extend(("--add-dir", str(directory)))
        argv.append(prompt)
        return argv

    async def run(self, prompt: str, *, cwd: Path, timeout_seconds: float) -> EngineResult:
        if timeout_seconds <= 0:
            raise ContractViolationError("timeout_seconds must be positive")
        started = time.monotonic()
        logger.info(
            "rsi.engine.started",
            engine="codex",
            model=CODEX_MODEL,
            reasoning_effort=CODEX_REASONING_EFFORT,
            sandbox=CODEX_SANDBOX,
            add_dirs=[str(directory) for directory in self._add_dirs],
            cwd=str(cwd),
        )
        refused = prompt_too_large(prompt, command=self._bin, started=started)
        if refused is not None:
            return refused
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
            return _start_failed(exc, command=self._bin, started=started)
        capped = await run_capped(proc, timeout_seconds=timeout_seconds)
        wall = time.monotonic() - started
        if capped.output_limit_exceeded:
            diagnostic = (
                "[rsi output limit exceeded; stdout/stderr capture was capped at "
                f"{DEFAULT_MAX_OUTPUT_BYTES} bytes per stream]"
            )
            stderr = capped.stderr.decode("utf-8", "replace")
            stderr = f"{stderr}\n{diagnostic}" if stderr else diagnostic
            logger.warning(
                "rsi.engine.output_limit_exceeded",
                command=self._bin,
                wall_seconds=wall,
                max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
            )
            return EngineResult(
                exit_code=OUTPUT_LIMIT_EXIT,
                stdout=capped.stdout.decode("utf-8", "replace"),
                stderr=stderr,
                wall_seconds=wall,
                timed_out=False,
            )
        if capped.timed_out:
            logger.warning(
                "rsi.engine.timed_out",
                command=self._bin,
                wall_seconds=wall,
                timeout=timeout_seconds,
            )
            return EngineResult(
                exit_code=_EXIT_TIMED_OUT,
                stdout=capped.stdout.decode("utf-8", "replace"),
                stderr=capped.stderr.decode("utf-8", "replace"),
                wall_seconds=wall,
                timed_out=True,
            )
        logger.info(
            "rsi.engine.finished",
            command=self._bin,
            exit_code=capped.exit_code,
            wall_seconds=round(wall, 3),
        )
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
