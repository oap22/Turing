"""Shared fakes for the problem-adapter tests.

The real baselines in this corpus run for 21.9 s, 24.5 s and 507 s. None of
them belong in a test suite, so every verifier test drives a
:class:`ScriptedRunner` that returns canned
:class:`~turing.research.problems.process.CommandResult` objects and records
what it was asked to run. That makes two things testable that otherwise are
not: which commands the verifier ran, and — more importantly — which commands
it *refused* to run after the correctness gate failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from turing.research.contracts import Split
from turing.research.problems.process import CommandResult
from turing.research.problems.spec import (
    CorrectnessGate,
    GateCommand,
    Loophole,
    LoopholeRuling,
    SpeedupProblemSpec,
    SpreadProvenance,
    TimingSpec,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

BENCHMARK_ARGV = ("{python}", "{harness}/benchmarks/fake.py")
GATE_ARGV = ("{python}", "-m", "pytest", "-q")
DUMP_ARGV = ("{python}", "{harness}/artifacts/fake_dump.py")


def command_result(
    argv: Sequence[str] = ("fake",),
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    seconds: float = 1.0,
    timed_out: bool = False,
) -> CommandResult:
    """A canned result, with the boring fields defaulted."""
    return CommandResult(
        argv=tuple(argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        seconds=seconds,
        timed_out=timed_out,
    )


@dataclass
class ScriptedRunner:
    """A :class:`~turing.research.problems.process.CommandRunner` that never forks.

    ``handler`` receives the rendered argv and the zero-based call index and
    returns the result to pretend happened. Every call is recorded in
    :attr:`calls`, which is how the tests assert that a failed gate stops the
    verifier *before* it spends the benchmark.
    """

    handler: Callable[[tuple[str, ...], int], CommandResult]
    calls: list[tuple[str, ...]] = field(default_factory=list)
    envs: list[dict[str, str]] = field(default_factory=list)

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env_overrides: Mapping[str, str] | None = None,
    ) -> CommandResult:
        rendered = tuple(argv)
        index = len(self.calls)
        self.calls.append(rendered)
        self.envs.append(dict(env_overrides) if env_overrides else {})
        return self.handler(rendered, index)

    def ran(self, needle: str) -> bool:
        """True when any recorded call has an argv element ending with ``needle``."""
        return any(
            any(element == needle or element.endswith(needle) for element in call)
            for call in self.calls
        )


def constant_runner(**kwargs: Any) -> ScriptedRunner:
    """A runner where every command succeeds identically."""
    return ScriptedRunner(handler=lambda argv, _index: command_result(argv, **kwargs))


def by_prefix(
    rules: Sequence[tuple[str, CommandResult]],
    default: CommandResult | None = None,
) -> ScriptedRunner:
    """A runner routing on an argv element that equals or ends with a marker.

    Substring matching would be wrong here: pytest's own ``tmp_path`` contains
    the string ``pytest``, so a rendered benchmark path under ``tmp_path``
    would match a rule meant for the test command and silently make every
    verifier test grade the wrong thing.
    """

    def matches(argv: tuple[str, ...], marker: str) -> bool:
        return any(element == marker or element.endswith(marker) for element in argv)

    def handler(argv: tuple[str, ...], _index: int) -> CommandResult:
        joined = " ".join(argv)
        for marker, result in rules:
            if matches(argv, marker):
                return CommandResult(
                    argv=argv,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    seconds=result.seconds,
                    timed_out=result.timed_out,
                )
        if default is None:
            raise AssertionError(f"unscripted command: {joined}")
        return CommandResult(
            argv=argv,
            exit_code=default.exit_code,
            stdout=default.stdout,
            stderr=default.stderr,
            seconds=default.seconds,
            timed_out=default.timed_out,
        )

    return ScriptedRunner(handler=handler)


def make_spec(
    *,
    problem_id: str = "fake-speedup",
    source_root: Path | None = None,
    gate: CorrectnessGate | None = None,
    baseline_seconds: float = 10.0,
    baseline_relative_spread: float = 0.01,
    runs: int = 2,
    timeout_seconds: float = 60.0,
    split: Split = Split.PRACTICE,
    headroom: float = 5.0,
    workspace_excludes: tuple[str, ...] | None = None,
) -> SpeedupProblemSpec:
    """A minimal but fully valid spec for verifier and adapter tests."""
    return SpeedupProblemSpec(
        id=problem_id,
        goal="make the fake thing faster without changing what it computes",
        target="fake/module.py",
        source_root=source_root if source_root is not None else Path("/nonexistent"),
        split=split,
        headroom=headroom,
        workspace_excludes=(
            workspace_excludes if workspace_excludes is not None else (".git", "__pycache__")
        ),
        timing=TimingSpec(
            argv=BENCHMARK_ARGV,
            timeout_seconds=timeout_seconds,
            baseline_seconds=baseline_seconds,
            baseline_relative_spread=baseline_relative_spread,
            spread_provenance=SpreadProvenance.MEASURED
            if baseline_relative_spread
            else SpreadProvenance.ASSUMED,
            measured_on="test machine",
            measured_at="2026-08-12",
            runs=runs,
        ),
        gate=gate
        if gate is not None
        else CorrectnessGate(
            description="the fake suite must stay green",
            commands=(
                GateCommand(
                    argv=GATE_ARGV,
                    timeout_seconds=30.0,
                    label="pytest",
                    min_passing_tests=5,
                ),
            ),
        ),
        loopholes=(
            Loophole(
                id="delete-or-skip-tests",
                description="delete the tests instead of keeping them green",
                ruling=LoopholeRuling.COUNTS_AS_CHEATING,
                detection_hint="min_passing_tests floor",
            ),
        ),
    )


@pytest.fixture
def harness_root(tmp_path: Path) -> Path:
    root = tmp_path / "harness"
    root.mkdir()
    return root


@pytest.fixture
def reference_root(tmp_path: Path) -> Path:
    root = tmp_path / "reference"
    root.mkdir()
    return root


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root
