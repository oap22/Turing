"""Subprocess execution for problem verifiers.

Every verifier in this package grades a workspace by *running things in it* —
a benchmark driver, ``pytest``, ``vitest``. This module is the one place that
knows how to do that, so the timing harness and the correctness gate agree on
what "the command failed" means.

Three deliberate choices:

* **argv only, never a shell.** Commands are declared as argument vectors and
  executed with :func:`asyncio.create_subprocess_exec`. A problem definition is
  data that describes how to grade the thing being graded; running it through a
  shell would turn every declaration into an injection surface pointing at the
  harness.
* **Timeouts are mandatory.** A benchmark whose baseline is 507 s can hang, and
  an unattended loop has nobody to notice. A timed-out command is a *failure*,
  never a fast result.
* **Output is captured and truncated from the front.** Test runners put the
  summary line and the traceback at the end, so the tail is the informative
  part.

The :class:`CommandRunner` protocol exists so verifier tests can substitute a
fake and never spend the 24-second or 507-second baselines in the test suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from turing.research.contracts import ContractViolationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

logger = structlog.get_logger("turing.research.problems.process")

__all__ = [
    "MAX_CAPTURED_OUTPUT",
    "CommandResult",
    "CommandRunner",
    "SubprocessCommandRunner",
    "count_passing_tests",
    "passing_test_counts",
    "pythonpath_for_workspace",
    "render_argv",
    "run_command",
]

#: Characters of stdout/stderr retained per stream. Kept from the tail.
MAX_CAPTURED_OUTPUT = 8000

#: Placeholders a declared argv may contain. Anything else is a typo, and a
#: typo'd placeholder that survives into a real command line silently grades
#: the wrong thing.
_KNOWN_PLACEHOLDERS = frozenset({"python", "workspace", "harness"})
_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

# vitest prints "Test Files  5 passed (5)" *before* "Tests  21 passed (21)";
# pytest -q prints a single "41 passed in 1.2s". Prefer the labelled vitest
# line. Never take the last generic match: the workspace owns stdout and can
# append a second summary (``atexit`` in ``conftest.py``) that would otherwise
# become the count.
_VITEST_TESTS_RE = re.compile(r"^\s*Tests\b.*?(\d+)\s+passed", re.MULTILINE)
_PASSED_RE = re.compile(r"(\d+)\s+passed")


def pythonpath_for_workspace(workspace: Path, *, existing: str | None = None) -> str:
    """Put the workspace's ``src`` first on ``PYTHONPATH``.

    Harness drivers are plain scripts under ``{harness}``. Pytest inserts
    ``<rootdir>/src`` via this repo's ``pythonpath = ["src"]``, but a driver
    never goes through pytest, so the editable install of the original checkout
    wins ``import turing`` and every candidate times unmodified source.
    ``--workspace`` is a path the driver can read; it is not an import path.
    Prepending ``workspace/src`` is what makes the copy the agent edited the
    copy that is measured.

    ``existing`` defaults to the current process ``PYTHONPATH`` so other
    entries survive behind the workspace. Pass ``existing=""`` to isolate.
    """
    inherited = os.environ.get("PYTHONPATH", "") if existing is None else existing
    prefix = str(workspace / "src")
    rest = [p for p in inherited.split(os.pathsep) if p and p != prefix]
    return os.pathsep.join([prefix, *rest])


def _tail(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= MAX_CAPTURED_OUTPUT:
        return text
    return "…<truncated>…\n" + text[-MAX_CAPTURED_OUTPUT:]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What one subprocess invocation did.

    ``seconds`` is wall-clock around the whole invocation, which is what the
    timing harness measures. It is deliberately *not* CPU time: the speedup
    problems include ONNX sessions, thread-pool configuration and a Node test
    runner, where the thing being optimised is exactly how well the work maps
    onto the machine.
    """

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        """True only for a clean exit inside the timeout."""
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> str:
        """One line naming the command and how it ended."""
        how = "timed out" if self.timed_out else f"exit {self.exit_code}"
        return f"{' '.join(self.argv)} → {how} in {self.seconds:.3f}s"


@runtime_checkable
class CommandRunner(Protocol):
    """Runs one command and reports how it went.

    The seam that keeps real baselines out of the test suite: a fake runner
    returns canned :class:`CommandResult` objects, so a verifier's whole
    decision path can be exercised without spending 507 seconds.
    """

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env_overrides: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Execute ``argv`` in ``cwd`` and return the result."""
        ...


async def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    env_overrides: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run ``argv`` in ``cwd``, capturing output and enforcing a timeout.

    Args:
        argv: Argument vector. Never passed through a shell.
        cwd: Working directory — for a verifier, the attempt's workspace.
        timeout_seconds: Hard wall-clock limit. Exceeding it kills the process
            group leader and reports ``timed_out=True``.
        env_overrides: Extra environment on top of the inherited one.

    Returns:
        A :class:`CommandResult`. Failure is reported, never raised: a verifier
        turns a failed command into a failed gate, not into an exception that
        would take down an unattended round.
    """
    if not argv:
        raise ContractViolationError("cannot run an empty argv")
    if timeout_seconds <= 0:
        raise ContractViolationError(f"timeout must be positive, got {timeout_seconds!r}")

    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)

    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        out, err = b"", b""
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
    elapsed = time.perf_counter() - started

    result = CommandResult(
        argv=tuple(argv),
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=_tail(out),
        stderr=_tail(err),
        seconds=elapsed,
        timed_out=timed_out,
    )
    logger.debug(
        "research.command.finished",
        argv=result.argv,
        exit_code=result.exit_code,
        seconds=round(result.seconds, 4),
        timed_out=result.timed_out,
    )
    return result


@dataclass(frozen=True, slots=True)
class SubprocessCommandRunner:
    """The real :class:`CommandRunner`: a thin object wrapper over :func:`run_command`.

    A class rather than the bare function so it can be a dataclass field
    default without any descriptor-binding subtlety, and so a caller can pin
    environment overrides (``PYTHONHASHSEED``, thread counts) that must apply
    to every command in a problem.
    """

    base_env: Mapping[str, str] = field(default_factory=dict)

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env_overrides: Mapping[str, str] | None = None,
    ) -> CommandResult:
        env: dict[str, str] = dict(self.base_env)
        if env_overrides:
            env.update(env_overrides)
        return await run_command(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            env_overrides=env or None,
        )


def render_argv(
    template: Sequence[str],
    *,
    workspace: Path,
    harness_root: Path,
    python_executable: str,
) -> tuple[str, ...]:
    """Resolve the placeholders in a declared argv.

    Three placeholders are recognised:

    ``{python}``
        The interpreter the harness should use. The workspace is a *copy* of a
        source repo with its virtualenv excluded, so the interpreter always
        comes from outside it.
    ``{workspace}``
        The attempt's working directory — the agent's write surface.
    ``{harness}``
        The read-only harness root holding benchmark drivers and artifact
        dumpers. **Benchmark drivers must live here and not in the workspace**:
        a driver inside the agent's write surface is a benchmark the agent can
        edit, which is the harness-integrity gate from the brief (§ Gates,
        Q11). This module resolves the path; enforcing that the agent cannot
        write to it is an OS-level concern outside this process.

    Raises:
        ContractViolationError: If an argv element carries an unknown
            placeholder. Silently leaving ``{harnes}`` in a command line would
            grade a path that does not exist, or worse, one that does.
    """
    substitutions = {
        "python": python_executable,
        "workspace": str(workspace),
        "harness": str(harness_root),
    }
    rendered: list[str] = []
    for element in template:
        for name in _PLACEHOLDER_RE.findall(element):
            if name not in _KNOWN_PLACEHOLDERS:
                raise ContractViolationError(
                    f"unknown placeholder {{{name}}} in argv element {element!r}; "
                    f"known placeholders are {sorted(_KNOWN_PLACEHOLDERS)}"
                )
        for name, value in substitutions.items():
            element = element.replace("{" + name + "}", value)
        rendered.append(element)
    return tuple(rendered)


def passing_test_counts(stdout: str, stderr: str) -> tuple[int, ...]:
    """Every passing-test summary found, in the order the runner printed them.

    Empty means no summary. More than one means the output is not a single
    runner summary — the demonstrated cheat is a workspace ``conftest.py``
    that ``atexit``-prints ``52 passed in 0.01s`` after pytest's real line.
    """
    combined = f"{stdout}\n{stderr}"
    labelled = _VITEST_TESTS_RE.findall(combined)
    if labelled:
        return tuple(int(n) for n in labelled)
    return tuple(int(n) for n in _PASSED_RE.findall(combined))


def count_passing_tests(stdout: str, stderr: str) -> int | None:
    """Parse how many tests a runner reported as passing, or ``None``.

    Supports ``pytest -q`` (``41 passed in 1.2s``) and ``vitest`` (``Tests  21
    passed (21)``). The vitest form is matched first because vitest prints a
    ``Test Files`` summary line above the ``Tests`` line and the file count is
    the wrong number.

    Returns ``None`` when there is no summary **or more than one**. Taking the
    last match used to be the parser, and that is how a two-line ``conftest.py``
    defeated the anti-deletion floor: pytest reported ``1 passed``, then
    ``atexit`` printed ``52 passed in 0.01s``, and the floor saw 52.

    This is a cheat guard, not telemetry. "Make the suite pass" and "delete the
    suite" are the same exit code, and deleting tests is the cheapest possible
    speedup for a workspace graded on a pytest gate. Pairing an exit-code check
    with a floor on a *single* passing count is what makes that route fail.
    """
    found = passing_test_counts(stdout, stderr)
    if len(found) != 1:
        return None
    return found[0]
