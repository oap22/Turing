"""Fakes for the round runner: a scripted solver, a stub verifier, a fake clock.

No network, no real model calls, no real subprocesses. The point of the fakes
is that the runner's own guarantees — cap enforcement, per-type cells,
refusals, lineage, escalation suspend/resume — are properties of the runner and
must hold against *any* solver, including a broken or adversarial one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from turing.research.contracts import (
    HARNESS_FAILURE_KEY,
    SCORE_SCALE_LEADERBOARD_PERCENTILE,
    SCORE_SCALE_SPEEDUP,
    Cap,
    EngineIdentity,
    EscalationDecision,
    EscalationVerdict,
    Problem,
    ProblemType,
    Split,
    VerificationResult,
    Verifier,
)
from turing.research.loop.protocols import SolverStep, SolverTask
from turing.research.loop.runner import RoundConfig, RoundRunner
from turing.research.loop.trajectory import TrajectoryStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from turing.research.contracts import Attempt, EscalationRequest


# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #


class FakeClock:
    """Deterministic time; every read advances it by one tick."""

    def __init__(self, start_ms: int = 1_700_000_000_000, tick_ms: int = 10) -> None:
        self._now = start_ms
        self._tick = tick_ms

    def now_ms(self) -> int:
        self._now += self._tick
        return self._now

    def monotonic(self) -> float:
        self._now += self._tick
        return self._now / 1000.0

    def advance(self, seconds: float) -> None:
        """Jump the clock forward without a read. Used to simulate operator wait."""
        self._now += int(seconds * 1000)


# --------------------------------------------------------------------------- #
# Verifier
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScriptedVerifier(Verifier):
    """Returns a canned score sequence; the last value repeats forever.

    ``frozen=True`` is mandatory — :class:`Problem` probes the verifier at bind
    time and refuses anything writable.
    """

    scores: tuple[float, ...] = (1.0,)
    correctness: tuple[bool, ...] = (True,)
    raises: bool = False
    harness_failure: bool = False
    calls: list[Path] = field(default_factory=list, compare=False)

    async def verify(self, workspace: Path) -> VerificationResult:
        if self.raises:
            raise RuntimeError("verifier harness exploded")
        index = min(len(self.calls), len(self.scores) - 1)
        correctness_index = min(len(self.calls), len(self.correctness) - 1)
        self.calls.append(workspace)
        measurements: dict[str, float] = {"seconds": 1.0}
        score = self.scores[index]
        passed = self.correctness[correctness_index]
        if self.harness_failure:
            measurements[HARNESS_FAILURE_KEY] = 1.0
            score = 0.0
            passed = False
        return VerificationResult(
            problem_id=self.problem_id,
            verifier_id=self.verifier_id,
            score=score,
            passed_correctness=passed,
            score_scale=self.score_scale,
            raw_measurements=measurements,
            detail=f"call {index}",
        )


def make_problem(
    problem_id: str,
    *,
    problem_type: ProblemType = ProblemType.SPEEDUP,
    split: Split = Split.PRACTICE,
    scores: Sequence[float] = (1.0,),
    correctness: Sequence[bool] = (True,),
    raises: bool = False,
    harness_failure: bool = False,
    template: Path | None = None,
    default_cap: Cap | None = None,
) -> Problem:
    scale = (
        SCORE_SCALE_SPEEDUP
        if problem_type is ProblemType.SPEEDUP
        else SCORE_SCALE_LEADERBOARD_PERCENTILE
    )
    verifier = ScriptedVerifier(
        verifier_id=f"v-{problem_id}",
        problem_id=problem_id,
        description=f"verifier for {problem_id}",
        score_scale=scale,
        scores=tuple(scores),
        correctness=tuple(correctness),
        raises=raises,
        harness_failure=harness_failure,
    )
    return Problem(
        id=problem_id,
        problem_type=problem_type,
        goal=f"make {problem_id} better",
        workspace_template=template or Path("/nonexistent/template"),
        verifier=verifier,
        split=split,
        default_cap=default_cap,
    )


# --------------------------------------------------------------------------- #
# Solver
# --------------------------------------------------------------------------- #


class FakeSolver:
    """Emits a scripted sequence of steps, repeating the last one forever."""

    def __init__(self, steps: Sequence[SolverStep] | None = None) -> None:
        self._steps = list(steps or [SolverStep(tokens=10, note="work")])
        self.calls: list[tuple[str, int]] = []

    async def step(self, task: SolverTask, attempt: Attempt) -> SolverStep:
        index = min(len(self.calls), len(self._steps) - 1)
        self.calls.append((task.id, attempt.step_index))
        return self._steps[index]


class ExplodingSolver:
    """Raises on every step — stands in for a broken backend."""

    def __init__(self) -> None:
        self.calls = 0

    async def step(self, task: SolverTask, attempt: Attempt) -> SolverStep:
        self.calls += 1
        raise RuntimeError("backend unavailable")


# --------------------------------------------------------------------------- #
# Workspaces and escalation
# --------------------------------------------------------------------------- #


class TempWorkspaceProvider:
    """Makes an empty directory per attempt; no template copying in tests."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.made: list[Path] = []

    async def materialise(self, problem: Problem, *, attempt_id: str) -> Path:
        path = self.root / attempt_id
        path.mkdir(parents=True, exist_ok=True)
        self.made.append(path)
        return path


class ScriptedEscalationChannel:
    """Replies with a queued decision; the last one repeats.

    Records every request so a test can assert the loop actually suspended.
    """

    def __init__(self, verdicts: Sequence[EscalationVerdict | EscalationDecision] | None = None):
        self._queue = list(verdicts or [EscalationVerdict.CONTINUE])
        self.requests: list[EscalationRequest] = []

    async def request_decision(self, request: EscalationRequest) -> EscalationDecision:
        index = min(len(self.requests), len(self._queue) - 1)
        self.requests.append(request)
        item = self._queue[index]
        if isinstance(item, EscalationDecision):
            return EscalationDecision(
                request_id=request.request_id,
                verdict=item.verdict,
                decided_at_ms=item.decided_at_ms,
                cap_extension=item.cap_extension,
            )
        return EscalationDecision(
            request_id=request.request_id,
            verdict=item,
            decided_at_ms=1,
        )


class NeverAnswersChannel:
    """A channel that would suspend forever — used to prove the loop waits."""

    def __init__(self) -> None:
        self.requests: list[EscalationRequest] = []

    async def request_decision(self, request: EscalationRequest) -> EscalationDecision:
        self.requests.append(request)
        raise AssertionError("should not be reached in tests that never answer")


class RecordingNtfyClient:
    """Stands in for ``NtfyAlertClient`` — same one-method surface, no HTTP."""

    def __init__(self) -> None:
        self.pushes: list[str] = []

    async def ntfy_push(self, content: str) -> None:
        self.pushes.append(content)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

ENGINE = EngineIdentity(
    backend="claude",
    orchestrator_model="opus",
    substep_model="haiku",
    scaffold_git_sha="abc1234",
)

DEFAULT_CAP = Cap(max_steps=3, max_tokens=10_000, max_wall_clock_seconds=600.0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> TrajectoryStore:
    return TrajectoryStore(tmp_path / "results", "test-loop", clock=clock)


@pytest.fixture
def workspaces(tmp_path: Path) -> TempWorkspaceProvider:
    return TempWorkspaceProvider(tmp_path / "workspaces")


def make_runner(
    *,
    solver: Any,
    store: TrajectoryStore,
    workspaces: TempWorkspaceProvider,
    clock: FakeClock,
    escalations: Any | None = None,
) -> RoundRunner:
    return RoundRunner(
        solver=solver,
        workspaces=workspaces,
        trajectory=store,
        escalations=escalations or ScriptedEscalationChannel(),
        clock=clock,
    )


def make_config(
    *,
    round_index: int = 0,
    run_id: str = "r00",
    parent_round_id: str | None = None,
    eval_set_hash: str = "",
    seed: int = 7,
    cap: Cap | None = None,
    **kwargs: Any,
) -> RoundConfig:
    return RoundConfig(
        round_index=round_index,
        run_id=run_id,
        parent_round_id=parent_round_id,
        eval_set_hash=eval_set_hash,
        engine=ENGINE,
        seed=seed,
        default_cap=cap or DEFAULT_CAP,
        **kwargs,
    )
