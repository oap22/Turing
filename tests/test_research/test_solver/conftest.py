"""Fakes and fixtures for the within-project solver.

Nothing here touches the network or a model API. The backend is a scripted
list or a deterministic ladder, the operator is a scripted queue of verdicts,
and the clock is advanced by hand — so a test that exercises a four-hour
wall-clock cap runs in microseconds.

The verifier is deliberately *real* in one respect: it reads the workspace off
disk. The apply → verify chain is where a resume bug would hide, so the tests
observe the same bytes the solver wrote rather than trusting a mock's memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest

from turing.research.contracts import (
    HARNESS_FAILURE_KEY,
    Attempt,
    Cap,
    CapConsumption,
    EscalationDecision,
    EscalationVerdict,
    Problem,
    ProblemType,
    Split,
    VerificationResult,
    Verifier,
)
from turing.research.solver import (
    FileEdit,
    InMemoryCheckpointStore,
    Proposal,
    WorkspaceManager,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from turing.research.contracts import EscalationRequest
    from turing.research.solver import IterationRecord, ProposalContext

SOLUTION_FILE = "solution.txt"


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class SimulatedCrashError(RuntimeError):
    """Stand-in for the process dying — a closed subscription window, a kill."""


class FakeClock:
    """A clock the test drives, so cap arithmetic needs no real waiting."""

    def __init__(self, now_ms: int = 1_700_000_000_000, monotonic: float = 0.0) -> None:
        self._now_ms = now_ms
        self._monotonic = monotonic

    def now_ms(self) -> int:
        return self._now_ms

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._now_ms += int(seconds * 1000)


@dataclass(frozen=True)
class NumberVerifier(Verifier):
    """Scores the number written into ``solution.txt``.

    Frozen, like every verifier: contracts refuse to bind a mutable one to a
    problem. Correctness and score are separate — an unparseable file is
    *incorrect* rather than merely low-scoring, which is the distinction
    :class:`~turing.research.contracts.VerificationResult` exists to preserve.
    """

    filename: str = SOLUTION_FILE

    async def verify(self, workspace: Path) -> VerificationResult:
        raw = ""
        try:
            raw = (workspace / self.filename).read_text(encoding="utf-8").strip()
            score = float(raw)
            passed = True
        except (OSError, ValueError):
            score, passed = 0.0, False
        return VerificationResult(
            problem_id=self.problem_id,
            verifier_id=self.verifier_id,
            score=score,
            passed_correctness=passed,
            score_scale=self.score_scale,
            raw_measurements={"parsed": score},
            detail=f"read {raw!r}",
        )


@dataclass(frozen=True)
class ExplodingVerifier(Verifier):
    """A verifier that cannot run — a broken harness, not a bad solution."""

    message: str = "timing harness is missing"

    async def verify(self, workspace: Path) -> VerificationResult:
        raise RuntimeError(self.message)


@dataclass(frozen=True)
class FlaggingVerifier(Verifier):
    """Does not raise. Sets ``HARNESS_FAILURE_KEY``, which is how SpeedupVerifier reports a dead harness.

    ``succeed_times`` grades that many workspaces for real (same as
    :class:`NumberVerifier`), then flags. The flagged result claims a passing
    score so a missing check would mark the attempt ``PASSED``.
    """

    succeed_times: int = 0
    flagged_score: float = 99.0
    flagged_correct: bool = True
    filename: str = SOLUTION_FILE
    calls: list[Path] = field(default_factory=list, compare=False)

    async def verify(self, workspace: Path) -> VerificationResult:
        n = len(self.calls)
        self.calls.append(workspace)
        if n >= self.succeed_times:
            return VerificationResult(
                problem_id=self.problem_id,
                verifier_id=self.verifier_id,
                score=self.flagged_score,
                passed_correctness=self.flagged_correct,
                score_scale=self.score_scale,
                raw_measurements={HARNESS_FAILURE_KEY: 1.0},
                detail="timing harness is missing",
            )
        raw = ""
        try:
            raw = (workspace / self.filename).read_text(encoding="utf-8").strip()
            score = float(raw)
            passed = True
        except (OSError, ValueError):
            score, passed = 0.0, False
        return VerificationResult(
            problem_id=self.problem_id,
            verifier_id=self.verifier_id,
            score=score,
            passed_correctness=passed,
            score_scale=self.score_scale,
            raw_measurements={"parsed": score},
            detail=f"read {raw!r}",
        )


@dataclass(frozen=True)
class MisattributingVerifier(Verifier):
    """Returns a result for some other problem. A harness-integrity failure."""

    async def verify(self, workspace: Path) -> VerificationResult:
        return VerificationResult(
            problem_id="some-other-problem",
            verifier_id=self.verifier_id,
            score=99.0,
            passed_correctness=True,
            score_scale=self.score_scale,
        )


class ScriptedBackend:
    """Returns a fixed list of proposals, one per call."""

    def __init__(
        self,
        proposals: Sequence[Proposal],
        *,
        hook: Callable[[ProposalContext], None] | None = None,
    ) -> None:
        self._proposals = list(proposals)
        self._hook = hook
        self.contexts: list[ProposalContext] = []

    @property
    def calls(self) -> int:
        return len(self.contexts)

    async def propose(self, context: ProposalContext) -> Proposal:
        self.contexts.append(context)
        if self._hook is not None:
            self._hook(context)
        if not self._proposals:
            raise AssertionError("backend script exhausted; the solver called once too often")
        return self._proposals.pop(0)


class LadderBackend:
    """Writes a steadily improving number into the workspace, forever.

    Used where the interesting thing is how many times the solver calls a
    backend rather than what it says — cap tests, mostly.
    """

    def __init__(
        self,
        *,
        start: float = 1.0,
        step: float = 1.0,
        tokens: int = 100,
        hook: Callable[[ProposalContext], None] | None = None,
    ) -> None:
        self._start = start
        self._step = step
        self._tokens = tokens
        self._hook = hook
        self.contexts: list[ProposalContext] = []

    @property
    def calls(self) -> int:
        return len(self.contexts)

    async def propose(self, context: ProposalContext) -> Proposal:
        self.contexts.append(context)
        if self._hook is not None:
            self._hook(context)
        value = self._start + self._step * context.iteration_index
        return Proposal(
            proposal_id=f"p{context.iteration_index}",
            rationale=f"try {value}",
            edits=(FileEdit(relative_path=SOLUTION_FILE, content=str(value)),),
            tokens=self._tokens,
        )


class ScriptedChannel:
    """A scripted operator. ``None`` means "nobody has answered yet"."""

    def __init__(self, replies: Sequence[Callable[[str], EscalationDecision] | None] = ()) -> None:
        self._replies = list(replies)
        self.requests: list[EscalationRequest] = []

    async def raise_escalation(self, request: EscalationRequest) -> None:
        self.requests.append(request)

    async def await_decision(self, request_id: str) -> EscalationDecision | None:
        if not self._replies:
            return None
        reply = self._replies.pop(0)
        return None if reply is None else reply(request_id)

    def queue(self, reply: Callable[[str], EscalationDecision] | None) -> None:
        self._replies.append(reply)


def verdict_continue(decided_at_ms: int = 1) -> Callable[[str], EscalationDecision]:
    def build(request_id: str) -> EscalationDecision:
        return EscalationDecision(
            request_id=request_id,
            verdict=EscalationVerdict.CONTINUE,
            decided_at_ms=decided_at_ms,
        )

    return build


def verdict_abandon(decided_at_ms: int = 1) -> Callable[[str], EscalationDecision]:
    def build(request_id: str) -> EscalationDecision:
        return EscalationDecision(
            request_id=request_id,
            verdict=EscalationVerdict.ABANDON,
            decided_at_ms=decided_at_ms,
        )

    return build


def verdict_extend(
    *,
    extra_steps: int = 0,
    extra_tokens: int = 0,
    extra_wall_clock_seconds: float = 0.0,
    decided_at_ms: int = 1,
) -> Callable[[str], EscalationDecision]:
    from turing.research.contracts import CapExtension

    def build(request_id: str) -> EscalationDecision:
        return EscalationDecision(
            request_id=request_id,
            verdict=EscalationVerdict.EXTEND_CAP,
            decided_at_ms=decided_at_ms,
            cap_extension=CapExtension(
                extra_steps=extra_steps,
                extra_tokens=extra_tokens,
                extra_wall_clock_seconds=extra_wall_clock_seconds,
            ),
        )

    return build


class CrashAfterStore:
    """Delegates to a real store, then dies at a chosen commit boundary.

    Crashes *after* the write lands, which is the interesting failure: the
    checkpoint is durable and the process is gone. Anything else would be
    testing the store rather than resume.
    """

    def __init__(self, inner: Any, *, crash_after_commits: int | None = None) -> None:
        self._inner = inner
        self._crash_after = crash_after_commits
        self.commits = 0

    async def save_attempt(self, attempt: Attempt) -> None:
        await self._inner.save_attempt(attempt)

    async def load_attempt(self, attempt_id: str) -> Attempt | None:
        result: Attempt | None = await self._inner.load_attempt(attempt_id)
        return result

    async def commit_iteration(self, record: IterationRecord, attempt: Attempt) -> None:
        await self._inner.commit_iteration(record, attempt)
        self.commits += 1
        if self._crash_after is not None and self.commits >= self._crash_after:
            raise SimulatedCrashError(
                f"process died after commit {self.commits} "
                f"(iteration {record.iteration_index}, phase {record.phase.value})"
            )

    async def latest_iteration(self, attempt_id: str) -> IterationRecord | None:
        result: IterationRecord | None = await self._inner.latest_iteration(attempt_id)
        return result

    async def iterations(self, attempt_id: str) -> tuple[IterationRecord, ...]:
        result: tuple[IterationRecord, ...] = await self._inner.iterations(attempt_id)
        return result

    async def save_escalation(self, request: EscalationRequest) -> None:
        await self._inner.save_escalation(request)

    async def load_escalation(self, request_id: str) -> EscalationRequest | None:
        result: EscalationRequest | None = await self._inner.load_escalation(request_id)
        return result


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def template_dir(tmp_path: Path) -> Path:
    template = tmp_path / "template"
    (template / "src").mkdir(parents=True)
    (template / "README.md").write_text("baseline\n", encoding="utf-8")
    (template / "src" / "baseline.py").write_text("def slow():\n    return 1\n", encoding="utf-8")
    return template


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "turing-workspace"


@pytest.fixture
def manager(workspace_root: Path) -> WorkspaceManager:
    return WorkspaceManager(workspace_root)


@pytest.fixture
def verifier() -> NumberVerifier:
    return NumberVerifier(
        verifier_id="v-speedup-1",
        problem_id="speedup-1",
        description="score is the number in solution.txt",
        score_scale="speedup_ratio",
    )


@pytest.fixture
def problem(template_dir: Path, verifier: NumberVerifier) -> Problem:
    return Problem(
        id="speedup-1",
        problem_type=ProblemType.SPEEDUP,
        goal="make it faster without changing the answer",
        workspace_template=template_dir,
        verifier=verifier,
        split=Split.PRACTICE,
    )


@pytest.fixture
def cap() -> Cap:
    return Cap(max_steps=10, max_tokens=100_000, max_wall_clock_seconds=3600.0)


@pytest.fixture
def store() -> InMemoryCheckpointStore:
    return InMemoryCheckpointStore()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def attempt(manager: WorkspaceManager, cap: Cap) -> Attempt:
    return Attempt(
        attempt_id="a1",
        problem_id="speedup-1",
        round_id="r0",
        seed=7,
        workspace_path=manager.path_for("speedup-1", "a1"),
        cap=cap,
    )


def fresh_with_cap(attempt: Attempt, cap: Cap) -> Attempt:
    """Rebuild a not-yet-started attempt with a different initial cap.

    ``Attempt.evolve`` refuses cap replacement. Tests that need a tighter
    budget construct it here rather than swapping a running meter.
    """
    if attempt.checkpoint_seq != 0 or attempt.consumed != CapConsumption():
        raise AssertionError(
            "fresh_with_cap is construction, not a cap swap; "
            "start the attempt with the intended cap"
        )
    return replace(attempt, cap=cap)
