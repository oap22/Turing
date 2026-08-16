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
    score_scale: str | None = None,
    score_floor: float | None = None,
) -> Problem:
    scale = score_scale or (
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
        score_floor=score_floor,
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


#: A step whose self-reported token count is not a finite number.
#:
#: ``SolverStep.tokens`` is self-reported — "only the backend can know them"
#: (``protocols.py``) — and is checked for sign, not for finiteness, so a
#: backend whose usage accounting returns an infinity or a NaN produces this
#: object with nothing refusing it. The runner charges it to
#: ``attempt.consumed``, and the next thing it does is checkpoint the attempt:
#: ``trajectory._write_json`` serialises with ``allow_nan=False`` — a
#: deliberate refusal, because "Python emits bare ``NaN``/``Infinity``, which
#: is not JSON: every other reader rejects the file outright" — so the write
#: raises ``ValueError`` out of ``run_attempt`` and lands in ``run_attempts``'
#: per-attempt containment.
#:
#: That makes it the way a test drives a contained attempt crash at a chosen
#: *step*, and so on a chosen side of the attempt's first metrics append: put
#: it first in a script and nothing is ever written; put it later and the
#: attempt dies with an intact chain already on disk. A solver *exception*
#: cannot do either — ``run_attempt`` catches those and escalates — and a
#: workspace failure can only do the first.
#:
#: It replaced a colliding ``score_scale`` in this role. That scale used to be
#: the canonical trigger, and it is now refused where the verifier declares
#: it, so such a problem can no longer be constructed at all.
UNACCOUNTABLE_STEP = SolverStep(
    tokens=float("inf"),  # type: ignore[arg-type]
    note="backend usage accounting returned a non-finite token count",
)


class PerProblemSolver:
    """Runs a script for one named problem; every other problem gets plain work.

    :class:`FakeSolver` walks one script across the whole round, so a test
    that needs exactly one problem to misbehave has to reason about corpus
    order to line the script up. This keys on ``task.id`` instead, which is
    both simpler and closer to what it stands for: a failure that is a
    property of the *problem* and therefore reproduces on every seed and
    every re-drive, not one that depends on where in the corpus it sits.
    """

    def __init__(self, problem_id: str, script: Sequence[SolverStep]) -> None:
        self.problem_id = problem_id
        self._script = list(script)
        self._seen = 0
        self.calls: list[tuple[str, int]] = []

    async def step(self, task: SolverTask, attempt: Attempt) -> SolverStep:
        self.calls.append((task.id, attempt.step_index))
        if task.id != self.problem_id:
            return SolverStep(tokens=10, note="work")
        index = min(self._seen, len(self._script) - 1)
        self._seen += 1
        return self._script[index]


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


class WorkspacesRefusingOneProblem(TempWorkspaceProvider):
    """Refuses one problem's workspace, on every seed and every round.

    Stands in for a *deterministic* per-problem loss: a workspace template
    that was pruned, evicted, or hit a full disk — the exact I/O
    ``runner.py`` names when it explains why a crash can land between
    materialisation and rotation. Materialisation is the earliest thing
    :meth:`RoundRunner.run_attempt` does, so this is the way to drive a
    contained crash *before* the attempt's first metrics append: nothing is
    written, and the directory is not a run at all.

    Keyed on ``problem.id`` rather than on the attempt id, so the loss is a
    property of the problem — identical on every seed and every re-drive —
    which is what makes it usable as the noise floor's deterministic case.
    """

    def __init__(self, root: Path, *, problem_ids: Sequence[str]) -> None:
        super().__init__(root)
        self.refused = list(problem_ids)
        self.refusals = 0

    async def materialise(self, problem: Problem, *, attempt_id: str) -> Path:
        if problem.id in self.refused:
            self.refusals += 1
            raise OSError(f"workspace template for {problem.id} is unreadable")
        return await super().materialise(problem, attempt_id=attempt_id)


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
