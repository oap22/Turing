"""``RoundRunner`` — one round of the corpus, fully unattended.

A round is: every problem in the corpus, one attempt each, verified by the
frozen verifier, reduced to per-``(type, split)`` cells, and written out as a
:class:`~turing.research.contracts.RoundRecord` plus a row in
``trajectory.json``. **This module is the experiment's instrument — if it is
wrong, every result is wrong**, so the invariants it enforces are listed here
and each one is a test.

**The runner owns what the solver must not.**

* *The cap.* One step is charged per :meth:`Solver.step` call regardless of
  what the solver reports, so the runaway brake works against a buggy solver.
  Wall-clock is measured on the runner's clock around the step *and* the
  verifier call — the solver's self-report is not what the cap reads, and
  harness time is not free. Time blocked on an operator decision is not
  charged (that wait is human-gate load, not budget). The cap is rechecked
  after every charge, before verification and before a pass or escalation is
  recorded, so an over-budget step cannot still land as ``PASSED``.
* *The verifier.* The runner calls it; the solver never holds it. The latest
  usable result is fed back through ``attempt.result`` as read-only context,
  which is the brief's "iterate against the frozen verifier" — reading the
  bar, never editing it. The reported score is that result, not a max over
  every verify: max-of-N on a timing harness grows with the cap at zero
  capability change.
* *Termination.* A solver can request an escalation; this call-site guard
  has no way to quit. :meth:`~turing.research.contracts.Attempt.evolve` can
  still reach ``ABANDONED`` with a leftover ``escalation_id``.

**Cap exhaustion terminates, it does not escalate** (unless
``escalate_on_cap_exhaustion`` is set). "Failed within cap" is a first-class,
logged outcome. Escalating every cap trip would pin human-gate load at one per
problem per round forever, and human-gate load — driving function #4 — is the
number the self-improvement claim lives or dies on. It must be free to fall to
zero.

**A binary state is not the measurement.** ``PASSED`` / ``FAILED_WITHIN_CAP``
records whether a binary bar was met, where one was declared. The reported
number is the continuous score, which exists either way.

**Scope: loop 1.** Nothing here self-edits. The single sanctioned path from a
round's results into a future self-edit summary is
:mod:`turing.research.loop.self_edit_seam`, which this module never calls.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

import structlog

from turing.research.contracts import (
    Attempt,
    AttemptState,
    CapConsumption,
    ContractViolationError,
    EscalationProtocolError,
    EscalationReason,
    EscalationRequest,
    EscalationVerdict,
    RoundCost,
    RoundRecord,
)
from turing.research.loop.metrics import (
    DEFAULT_SCORE_FLOORS,
    CostBasis,
    ScoredProblem,
    assess_saturation,
    build_type_scores,
    compute_deltas,
    floors_by_cell,
    round_verdict,
)
from turing.research.loop.protocols import SolverTask, SystemClock
from turing.research.loop.trajectory import AttemptLog, StepLog
from turing.research.problems.adapter import bind_eval_set_hash

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from turing.research.contracts import (
        Cap,
        EngineIdentity,
        EscalationDecision,
        Problem,
        TypeScore,
        VerificationResult,
    )
    from turing.research.loop.metrics import NoiseFloor, SaturationAssessment
    from turing.research.loop.noise_floor import NoiseFloorReport
    from turing.research.loop.protocols import (
        Clock,
        EscalationChannel,
        Solver,
        WorkspaceProvider,
    )
    from turing.research.loop.trajectory import TrajectoryStore

logger = structlog.get_logger(__name__)

__all__ = [
    "AttemptOutcome",
    "PassCriterion",
    "RoundConfig",
    "RoundOutcome",
    "RoundRunner",
]


@dataclass(frozen=True, slots=True)
class PassCriterion:
    """When a continuous score counts as "the verifier passed".

    Optional per problem. With **no** criterion an attempt runs until the cap
    and lands in ``FAILED_WITHIN_CAP`` holding its best score — which is the
    brief's solver shape ("build a solution, score it, revise, repeat until the
    cap") and is not a failure in the ordinary sense. Correctness alone is a
    poor default bar: a speedup problem's untouched baseline is already
    correct, so an attempt would "pass" before doing any work.
    """

    min_score: float | None = None
    require_correctness: bool = True

    def __post_init__(self) -> None:
        if self.min_score is None and not self.require_correctness:
            raise ContractViolationError(
                "a pass criterion that requires neither correctness nor a score is not a "
                "bar; omit the criterion instead and let the attempt run to its cap"
            )

    def satisfied_by(self, result: VerificationResult) -> bool:
        if self.require_correctness and not result.passed_correctness:
            return False
        if self.min_score is None:
            return self.require_correctness
        return result.score >= self.min_score


@dataclass(frozen=True, slots=True)
class RoundConfig:
    """Everything one round needs that is not the corpus.

    ``run_id`` doubles as the round id: attempts record it as their
    ``round_id`` and the next round records it as ``parent_round_id``, so
    lineage lives in one namespace.
    """

    round_index: int
    run_id: str
    parent_round_id: str | None
    #: Empty means derive from the corpus at :meth:`RoundRunner.run_round`.
    #: A non-empty value is verified against that fingerprint, never trusted.
    eval_set_hash: str
    engine: EngineIdentity
    seed: int
    default_cap: Cap
    cost_basis: CostBasis = CostBasis.WALL_CLOCK
    escalate_on_cap_exhaustion: bool = False
    #: Caps how many times a tripped cap may escalate when
    #: ``escalate_on_cap_exhaustion`` is set. It does **not** override an
    #: operator ``CONTINUE`` — that would be a self-quit, and it would
    #: mislabel the stop as ``FAILED_WITHIN_CAP`` with the cap untouched.
    max_escalations_per_attempt: int = 3
    pass_criteria: Mapping[str, PassCriterion] = field(default_factory=dict)
    score_floors: Mapping[str, float] = field(default_factory=lambda: DEFAULT_SCORE_FLOORS)
    #: When False, skip per-step verification and run the frozen verifier once
    #: when the cap trips. Never verifying floors every cell and fabricates a
    #: parent-round delta.
    verify_every_step: bool = True

    def __post_init__(self) -> None:
        if self.round_index < 0:
            raise ContractViolationError("round_index cannot be negative")
        if self.round_index == 0 and self.parent_round_id is not None:
            raise ContractViolationError("round 0 is the baseline and has no parent")
        if self.round_index > 0 and self.parent_round_id is None:
            raise ContractViolationError(
                f"round {self.round_index} must record its parent; a trajectory without "
                "lineage cannot distinguish accumulate from replace"
            )
        if self.max_escalations_per_attempt < 1:
            raise ContractViolationError("an attempt must be allowed at least one escalation")
        object.__setattr__(self, "pass_criteria", MappingProxyType(dict(self.pass_criteria)))
        object.__setattr__(self, "score_floors", MappingProxyType(dict(self.score_floors)))

    def criterion_for(self, problem_id: str) -> PassCriterion | None:
        return self.pass_criteria.get(problem_id)


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """One problem's result, and how it got there."""

    problem: Problem
    attempt: Attempt
    best_result: VerificationResult | None
    steps: tuple[StepLog, ...]
    escalations: tuple[EscalationRequest, ...]
    decisions: tuple[EscalationDecision, ...]
    attempt_log_path: Path | None = None

    @property
    def escalation_count(self) -> int:
        return len(self.escalations)


@dataclass(frozen=True, slots=True)
class RoundOutcome:
    """The round record plus everything that produced it."""

    record: RoundRecord
    attempts: tuple[AttemptOutcome, ...]
    scored: tuple[ScoredProblem, ...]
    assessments: tuple[SaturationAssessment, ...]
    trajectory_row: Mapping[str, Any] | None = None


def _is_better(new: VerificationResult, old: VerificationResult | None) -> bool:
    """Whether ``new`` should replace the attempt's reported result.

    A harness-failure result is not a grade and must never become ``best``.
    Correctness dominates: a fast wrong answer never replaces a correct one.
    Among results with the same correctness, the **latest** workspace wins —
    not the higher score. Max-of-N over a timing harness grows with the cap
    at zero capability change, and ``extend_cap`` would couple an operator
    decision to the primary score.
    """
    if new.harness_failed:
        return False
    if old is None:
        return True
    if new.passed_correctness != old.passed_correctness:
        return new.passed_correctness
    return True


def _spend_carried_on_error(exc: BaseException) -> CapConsumption:
    """Accounted backend spend attached to a step that then raised.

    Tokens come from the error's accounted ``usage`` or ``tokens``, never
    from model-authored JSON. A proposal call that happened still costs a
    step even when parse/apply then failed.
    """
    usage = getattr(exc, "usage", None)
    if usage is not None:
        accounted = getattr(usage, "total_tokens", 0)
        tokens = int(accounted) if accounted else 0
        return CapConsumption(steps=1, tokens=max(0, tokens))
    carried = getattr(exc, "tokens", None)
    if isinstance(carried, int) and carried > 0:
        return CapConsumption(steps=1, tokens=carried)
    return CapConsumption()


class RoundRunner:
    """Runs one round of the corpus. Unattended except for escalations."""

    def __init__(
        self,
        *,
        solver: Solver,
        workspaces: WorkspaceProvider,
        trajectory: TrajectoryStore,
        escalations: EscalationChannel,
        clock: Clock | None = None,
    ) -> None:
        self._solver = solver
        self._workspaces = workspaces
        self._trajectory = trajectory
        self._escalations = escalations
        self._clock = clock or SystemClock()
        self._operator_wait_seconds = 0.0

    @property
    def clock(self) -> Clock:
        """The injected clock, so callers timestamp against the same one."""
        return self._clock

    # -- one attempt -------------------------------------------------------- #

    async def run_attempt(
        self,
        problem: Problem,
        config: RoundConfig,
        *,
        output_dir: Path,
    ) -> AttemptOutcome:
        """Work one problem until it passes, the cap trips, or it is abandoned."""
        attempt_id = f"{config.run_id}-{problem.id}-{uuid.uuid4().hex[:8]}"
        workspace = await self._workspaces.materialise(problem, attempt_id=attempt_id)
        now = self._clock.now_ms()
        attempt = Attempt(
            attempt_id=attempt_id,
            problem_id=problem.id,
            round_id=config.run_id,
            seed=config.seed,
            workspace_path=workspace,
            cap=problem.default_cap or config.default_cap,
            state=AttemptState.PENDING,
            started_at_ms=now,
            updated_at_ms=now,
        )
        attempt = attempt.resume(now_ms=now)

        steps: list[StepLog] = []
        escalations: list[EscalationRequest] = []
        decisions: list[EscalationDecision] = []
        best: VerificationResult | None = None
        criterion = config.criterion_for(problem.id)

        while True:
            attempt, cap_action = await self._enforce_cap(
                problem=problem,
                attempt=attempt,
                best=best,
                config=config,
                output_dir=output_dir,
                escalations=escalations,
                decisions=decisions,
            )
            if cap_action == "break":
                break
            if cap_action == "continue":
                continue

            step_started = self._clock.monotonic()
            try:
                step = await self._solver.step(SolverTask.from_problem(problem), attempt)
            except Exception as exc:  # solver/backend blew up — a harness failure
                logger.exception(
                    "research.attempt.solver_failed",
                    problem_id=problem.id,
                    attempt_id=attempt.attempt_id,
                )
                attempt = self._charge_failed_step(attempt, step_started, exc)
                attempt, cap_action = await self._enforce_cap(
                    problem=problem,
                    attempt=attempt,
                    best=best,
                    config=config,
                    output_dir=output_dir,
                    escalations=escalations,
                    decisions=decisions,
                )
                if cap_action == "break":
                    break
                if cap_action == "continue":
                    continue
                attempt, _ = await self._escalate_or_fail(
                    problem=problem,
                    attempt=attempt,
                    reason=EscalationReason.HARNESS_FAILURE,
                    summary=f"solver raised {type(exc).__name__}: {exc}",
                    best=best,
                    config=config,
                    output_dir=output_dir,
                    escalations=escalations,
                    decisions=decisions,
                )
                if attempt.is_terminal:
                    break
                continue

            step_elapsed = self._elapsed(step_started)
            now = self._clock.now_ms()
            attempt = attempt.record_consumption(
                CapConsumption(steps=1, tokens=step.tokens, wall_clock_seconds=step_elapsed),
                now_ms=now,
            )
            attempt = attempt.evolve(
                now_ms=now,
                step_index=attempt.step_index + 1,
                resume_token=step.resume_token
                if step.resume_token is not None
                else attempt.resume_token,
            )

            result: VerificationResult | None = None
            verifier_error: str | None = None
            verify_elapsed = 0.0
            # Per-step verify is skipped once the cap is already gone (unit 14).
            # ``verify_every_step=False`` still verifies once, on that trip —
            # otherwise ``best`` stays None and the problem enters its cell at
            # the scale floor, fabricating a parent-round delta.
            verify_now = (config.verify_every_step and not attempt.cap_exhausted) or (
                not config.verify_every_step and attempt.cap_exhausted and best is None
            )
            if verify_now:
                attempt = attempt.evolve(now_ms=now, state=AttemptState.VERIFYING)
                verify_started = self._clock.monotonic()
                try:
                    result = await problem.verifier.verify(attempt.workspace_path)
                except Exception as exc:
                    verifier_error = f"{type(exc).__name__}: {exc}"
                    logger.exception(
                        "research.attempt.verifier_failed",
                        problem_id=problem.id,
                        attempt_id=attempt.attempt_id,
                    )
                else:
                    if _is_better(result, best):
                        best = result
                verify_elapsed = self._elapsed(verify_started)
                now = self._clock.now_ms()
                attempt = attempt.record_consumption(
                    CapConsumption(wall_clock_seconds=verify_elapsed), now_ms=now
                )
                attempt = attempt.evolve(
                    now_ms=now,
                    state=AttemptState.RUNNING,
                    result=best,
                )

            steps.append(
                StepLog(
                    index=attempt.step_index,
                    at_ms=now,
                    tokens=step.tokens,
                    wall_clock_seconds=step_elapsed + verify_elapsed,
                    note=step.note,
                    made_progress=step.made_progress,
                    escalate_requested=None if step.escalate is None else step.escalate.value,
                    score=None if result is None else result.score,
                    passed_correctness=None if result is None else result.passed_correctness,
                    verifier_error=verifier_error,
                )
            )
            await self._trajectory.write_attempt_checkpoint(attempt, output_dir=output_dir)

            if verifier_error is not None:
                attempt, _ = await self._escalate_or_fail(
                    problem=problem,
                    attempt=attempt,
                    reason=EscalationReason.VERIFIER_UNRUNNABLE,
                    summary=f"verifier {problem.verifier_id} failed: {verifier_error}",
                    best=best,
                    config=config,
                    output_dir=output_dir,
                    escalations=escalations,
                    decisions=decisions,
                )
                if attempt.is_terminal:
                    break
                continue

            if result is not None and result.harness_failed:
                logger.warning(
                    "research.attempt.harness_failure",
                    problem_id=problem.id,
                    attempt_id=attempt.attempt_id,
                    detail=result.detail,
                )
                attempt, _ = await self._escalate_or_fail(
                    problem=problem,
                    attempt=attempt,
                    reason=EscalationReason.HARNESS_FAILURE,
                    summary=result.detail or "verifier reported a harness failure",
                    best=best,
                    config=config,
                    output_dir=output_dir,
                    escalations=escalations,
                    decisions=decisions,
                )
                if attempt.is_terminal:
                    break
                continue

            attempt, cap_action = await self._enforce_cap(
                problem=problem,
                attempt=attempt,
                best=best,
                config=config,
                output_dir=output_dir,
                escalations=escalations,
                decisions=decisions,
            )
            if cap_action == "break":
                break
            if cap_action == "continue":
                continue

            if result is not None and criterion is not None and criterion.satisfied_by(result):
                logger.info(
                    "research.attempt.passed",
                    problem_id=problem.id,
                    attempt_id=attempt.attempt_id,
                    score=result.score,
                    steps=attempt.consumed.steps,
                )
                attempt = self._finish(attempt, AttemptState.PASSED, best)
                break

            if step.escalate is not None:
                attempt, _ = await self._escalate_or_fail(
                    problem=problem,
                    attempt=attempt,
                    reason=step.escalate,
                    summary=step.note or f"solver requested {step.escalate.value}",
                    best=best,
                    config=config,
                    output_dir=output_dir,
                    escalations=escalations,
                    decisions=decisions,
                )
                if attempt.is_terminal:
                    break

        await self._trajectory.write_attempt_checkpoint(attempt, output_dir=output_dir)
        log = AttemptLog(
            problem_id=problem.id,
            attempt_id=attempt.attempt_id,
            round_id=attempt.round_id,
            seed=attempt.seed,
            split=problem.split.value,
            problem_type=problem.problem_type.value,
            final_state=attempt.state.value,
            steps=tuple(steps),
            best_score=None if best is None else best.score,
            best_passed_correctness=None if best is None else best.passed_correctness,
            score_scale=problem.verifier.score_scale,
            consumed_steps=attempt.consumed.steps,
            consumed_tokens=attempt.consumed.tokens,
            consumed_wall_clock_seconds=attempt.consumed.wall_clock_seconds,
            cap_extensions=attempt.cap.extension_count,
            escalation_ids=tuple(e.request_id for e in escalations),
            workspace_path=str(attempt.workspace_path),
        )
        path = await self._trajectory.write_attempt_log(log, output_dir=output_dir)
        return AttemptOutcome(
            problem=problem,
            attempt=attempt,
            best_result=best,
            steps=tuple(steps),
            escalations=tuple(escalations),
            decisions=tuple(decisions),
            attempt_log_path=path,
        )

    def _finish(
        self,
        attempt: Attempt,
        state: AttemptState,
        best: VerificationResult | None,
    ) -> Attempt:
        return attempt.evolve(now_ms=self._clock.now_ms(), state=state, result=best)

    async def _enforce_cap(
        self,
        *,
        problem: Problem,
        attempt: Attempt,
        best: VerificationResult | None,
        config: RoundConfig,
        output_dir: Path,
        escalations: list[EscalationRequest],
        decisions: list[EscalationDecision],
    ) -> tuple[Attempt, Literal["ok", "break", "continue"]]:
        """Stop or extend as soon as a charge trips the cap — not at the next loop head.

        Rechecking only at the top of the loop lets an over-budget step still
        verify, pass, or escalate. After a charge, this is the next thing that
        runs.
        """
        if not attempt.cap_exhausted:
            return attempt, "ok"
        if (
            config.escalate_on_cap_exhaustion
            and len(escalations) < config.max_escalations_per_attempt
        ):
            attempt, decision = await self._escalate(
                problem=problem,
                attempt=attempt,
                reason=EscalationReason.CAP_EXHAUSTED,
                summary=self._cap_summary(attempt),
                best=best,
                config=config,
                output_dir=output_dir,
                escalations=escalations,
                decisions=decisions,
            )
            if attempt.is_terminal:
                return attempt, "break"
            if attempt.cap_exhausted:
                # CONTINUE with no budget left cannot mean "keep
                # working"; record the honest outcome rather than
                # spinning on an exhausted cap.
                logger.warning(
                    "research.attempt.continue_without_budget",
                    problem_id=problem.id,
                    attempt_id=attempt.attempt_id,
                    verdict=decision.verdict.value if decision else None,
                )
                return self._finish(attempt, AttemptState.FAILED_WITHIN_CAP, best), "break"
            return attempt, "continue"
        logger.info(
            "research.attempt.cap_exhausted",
            problem_id=problem.id,
            attempt_id=attempt.attempt_id,
            dimensions=[d.value for d in attempt.exceeded_cap_dimensions],
            steps=attempt.consumed.steps,
            tokens=attempt.consumed.tokens,
        )
        return self._finish(attempt, AttemptState.FAILED_WITHIN_CAP, best), "break"

    def _elapsed(self, started: float) -> float:
        return max(0.0, self._clock.monotonic() - started)

    def _charge_failed_step(self, attempt: Attempt, started: float, exc: BaseException) -> Attempt:
        """Charge wall-clock, plus any backend spend the error carried.

        ``generate`` records usage before adapter parse / apply / command
        can raise. If that spend stayed only on the backend ledger,
        operator CONTINUE would retry with it omitted. Tokens on the
        error are accounted usage, never a model-authored JSON field.
        A proposal call that happened still costs a step.
        """
        spend = _spend_carried_on_error(exc)
        return attempt.record_consumption(
            CapConsumption(
                steps=spend.steps,
                tokens=spend.tokens,
                wall_clock_seconds=self._elapsed(started),
            ),
            now_ms=self._clock.now_ms(),
        )

    @staticmethod
    def _cap_summary(attempt: Attempt) -> str:
        dims = ", ".join(d.value for d in attempt.exceeded_cap_dimensions) or "none"
        return (
            f"cap exhausted on {dims} after {attempt.consumed.steps} steps / "
            f"{attempt.consumed.tokens} tokens"
        )

    async def _escalate_or_fail(
        self,
        *,
        problem: Problem,
        attempt: Attempt,
        reason: EscalationReason,
        summary: str,
        best: VerificationResult | None,
        config: RoundConfig,
        output_dir: Path,
        escalations: list[EscalationRequest],
        decisions: list[EscalationDecision],
    ) -> tuple[Attempt, EscalationDecision | None]:
        """Escalate. This call-site guard has no quit path of its own.

        ``max_escalations_per_attempt`` does not terminate here. Stopping after
        N operator ``CONTINUE`` verdicts would both override the operator and
        write ``FAILED_WITHIN_CAP`` with the cap untouched — a false outcome
        that truncates driving function #4 by exactly the worst problems.
        Only an operator ``ABANDON`` or a tripped cap ends the attempt
        through this writer; :meth:`~turing.research.contracts.Attempt.evolve`
        can still reach ``ABANDONED`` with a leftover ``escalation_id``.
        """
        if len(escalations) >= config.max_escalations_per_attempt:
            logger.warning(
                "research.attempt.escalation_budget_spent",
                problem_id=problem.id,
                attempt_id=attempt.attempt_id,
                escalations=len(escalations),
                reason=reason.value,
            )
        return await self._escalate(
            problem=problem,
            attempt=attempt,
            reason=reason,
            summary=summary,
            best=best,
            config=config,
            output_dir=output_dir,
            escalations=escalations,
            decisions=decisions,
        )

    async def _escalate(
        self,
        *,
        problem: Problem,
        attempt: Attempt,
        reason: EscalationReason,
        summary: str,
        best: VerificationResult | None,
        config: RoundConfig,
        output_dir: Path,
        escalations: list[EscalationRequest],
        decisions: list[EscalationDecision],
    ) -> tuple[Attempt, EscalationDecision | None]:
        """Suspend the loop on an operator decision, then resume or terminate."""
        now = self._clock.now_ms()
        request = EscalationRequest(
            request_id=f"esc-{uuid.uuid4().hex[:12]}",
            problem_id=problem.id,
            attempt_id=attempt.attempt_id,
            round_id=attempt.round_id,
            reason=reason,
            summary=summary,
            cap=attempt.cap,
            consumed=attempt.consumed,
            created_at_ms=now,
            best_result=best,
        )
        escalations.append(request)
        # ESCALATED is neither terminal nor resumable — only an operator
        # decision moves it — so the checkpoint is written *before* suspending:
        # a crash while waiting resumes here rather than restarting the attempt.
        attempt = attempt.evolve(
            now_ms=now,
            state=AttemptState.ESCALATED,
            escalation_id=request.request_id,
            result=best,
        )
        await self._trajectory.write_attempt_checkpoint(attempt, output_dir=output_dir)
        await self._trajectory.write_escalation(request, None, output_dir=output_dir)
        logger.warning(
            "research.attempt.escalated",
            problem_id=problem.id,
            attempt_id=attempt.attempt_id,
            request_id=request.request_id,
            reason=reason.value,
        )

        waited_from = self._clock.monotonic()
        try:
            decision = await self._escalations.request_decision(request)
        finally:
            self._operator_wait_seconds += max(0.0, self._clock.monotonic() - waited_from)
        if decision.request_id != request.request_id:
            raise EscalationProtocolError(
                f"decision {decision.request_id!r} does not answer open request "
                f"{request.request_id!r}"
            )
        decisions.append(decision)
        await self._trajectory.write_escalation(request, decision, output_dir=output_dir)
        now = self._clock.now_ms()

        if decision.verdict is EscalationVerdict.ABANDON:
            logger.warning(
                "research.attempt.abandoned",
                problem_id=problem.id,
                attempt_id=attempt.attempt_id,
                request_id=request.request_id,
            )
            attempt = attempt.evolve(now_ms=now, state=AttemptState.ABANDONED, result=best)
            await self._trajectory.write_attempt_checkpoint(attempt, output_dir=output_dir)
            return attempt, decision

        # CONTINUE leaves the cap alone; EXTEND_CAP grows it via the
        # sanctioned path. `evolve` rather than `resume` because ESCALATED is
        # deliberately not resumable: this is the operator's hand on the
        # switch, not the agent's. Clear the bound id — a leftover one
        # would let evolve(state=ABANDONED) succeed without a new verdict.
        if decision.verdict is EscalationVerdict.EXTEND_CAP:
            assert decision.cap_extension is not None  # guaranteed by EscalationDecision
            attempt = attempt.apply_cap_extension(decision.cap_extension, now_ms=now)
        attempt = attempt.evolve(now_ms=now, state=AttemptState.RUNNING, escalation_id=None)
        await self._trajectory.write_attempt_checkpoint(attempt, output_dir=output_dir)
        logger.info(
            "research.attempt.resumed",
            problem_id=problem.id,
            attempt_id=attempt.attempt_id,
            request_id=request.request_id,
            verdict=decision.verdict.value,
            cap_extensions=attempt.cap.extension_count,
        )
        return attempt, decision

    # -- a whole round ------------------------------------------------------ #

    async def run_attempts(
        self,
        corpus: Sequence[Problem],
        config: RoundConfig,
        *,
        output_dir: Path,
    ) -> tuple[AttemptOutcome, ...]:
        """Run every problem once, sequentially. No trajectory row is written.

        Sequential because the brief buys opportunistic subscription use with
        "no parallel sweep"; it also keeps the round's wall clock a meaningful
        cost number (machine time, not operator sleep — see :meth:`run_round`).

        Used directly by the noise-floor runner, whose seed runs are *not*
        rounds and must not enter ``trajectory.json``.
        """
        if not corpus:
            raise ContractViolationError("a round with no problems measures nothing")
        outcomes: list[AttemptOutcome] = []
        for problem in corpus:
            outcomes.append(await self.run_attempt(problem, config, output_dir=output_dir))
        return tuple(outcomes)

    @staticmethod
    def _floors_from_report(
        eval_set_hash: str,
        engine: EngineIdentity,
        noise_floor: NoiseFloorReport | None,
    ) -> tuple[NoiseFloor, ...]:
        """Bind a provenanced floor to this round, or refuse.

        A bare sequence of :class:`NoiseFloor` values has no eval-set hash and
        no engine. Accepting one would let a floor from anywhere license a
        gain — the gate that exists so "no noise floor, no verdict" cannot be
        satisfied by a number from a different corpus.
        """
        if noise_floor is None:
            return ()
        report_hash = getattr(noise_floor, "eval_set_hash", None)
        report_engine = getattr(noise_floor, "engine", None)
        floors = getattr(noise_floor, "floors", None)
        if not isinstance(report_hash, str) or report_engine is None or floors is None:
            raise ContractViolationError(
                "a noise floor must be a NoiseFloorReport bound to an eval set and "
                "engine; a bare sequence of floors has no provenance and a floor "
                "from anywhere would license every gain"
            )
        if report_hash != eval_set_hash:
            raise ContractViolationError(
                f"noise floor eval_set_hash {report_hash!r} does not match this "
                f"round's {eval_set_hash!r}; a floor from a different corpus "
                "is not a floor"
            )
        if report_engine != engine:
            raise ContractViolationError(
                "noise floor engine does not match this round's engine; a floor "
                "measured on a different scaffold is not a floor"
            )
        return tuple(floors)

    async def run_round(
        self,
        corpus: Sequence[Problem],
        config: RoundConfig,
        *,
        parent: RoundRecord | None = None,
        noise_floor: NoiseFloorReport | None = None,
    ) -> RoundOutcome:
        """Run the round, reduce it to cells, and append the trajectory row.

        Round wall-clock cost is machine time only. Time spent blocked in
        :meth:`EscalationChannel.request_decision` is subtracted, so driving
        function #3 cannot improve merely because the operator was interrupted
        less (that signal belongs to human-gate load).

        ``noise_floor`` must be a :class:`NoiseFloorReport` (eval set + engine
        bound). A bare sequence of floors has no provenance; a floor from
        anywhere would license every gain.
        """
        await self._trajectory.ensure_layout()
        output_dir = self._trajectory.round_dir(config.round_index)
        self._operator_wait_seconds = 0.0
        if not corpus:
            raise ContractViolationError("a round with no problems measures nothing")
        eval_set_hash = bind_eval_set_hash(corpus, config.eval_set_hash)
        floors_seq = self._floors_from_report(eval_set_hash, config.engine, noise_floor)
        started = self._clock.monotonic()
        logger.info(
            "research.round.started",
            round_index=config.round_index,
            run_id=config.run_id,
            parent_round_id=config.parent_round_id,
            eval_set_hash=eval_set_hash,
            problems=len(corpus),
            seed=config.seed,
            noise_floors=len(floors_seq),
        )
        if not floors_seq:
            logger.warning(
                "research.round.no_noise_floor",
                round_index=config.round_index,
                detail=(
                    "no seed-noise floor supplied; marginal gain cannot be called signal "
                    "or noise and the saturation verdict will be refused"
                ),
            )

        outcomes = await self.run_attempts(corpus, config, output_dir=output_dir)
        elapsed = max(0.0, self._clock.monotonic() - started - self._operator_wait_seconds)

        scored = tuple(
            ScoredProblem.from_result(o.problem, o.best_result, score_floors=config.score_floors)
            for o in outcomes
        )
        type_scores = build_type_scores(scored)
        cost = RoundCost(
            wall_clock_seconds=elapsed,
            tokens=sum(o.attempt.consumed.tokens for o in outcomes),
            attempts=len(outcomes),
        )
        escalation_count = sum(o.escalation_count for o in outcomes)

        parent_scores: tuple[TypeScore, ...] | None = None
        comparable = True
        if parent is not None:
            if config.parent_round_id != parent.run_id:
                raise ContractViolationError(
                    f"round {config.run_id!r} records parent {config.parent_round_id!r} but "
                    f"was handed round {parent.run_id!r} to compare against; a trajectory "
                    "whose lineage disagrees with its deltas is worse than none"
                )
            parent_scores = parent.type_scores
            comparable = parent.eval_set_hash == eval_set_hash
            if not comparable:
                logger.error(
                    "research.round.eval_set_changed",
                    round_index=config.round_index,
                    parent_eval_set_hash=parent.eval_set_hash,
                    eval_set_hash=eval_set_hash,
                    detail="rounds measured on different eval sets are not comparable",
                )

        floors = floors_by_cell(floors_seq)
        deltas = compute_deltas(
            type_scores,
            parent_scores,
            floors,
            cost=cost,
            basis=config.cost_basis,
            comparable=comparable,
        )
        assessments = assess_saturation(type_scores, parent_scores, floors, comparable=comparable)
        gates = {
            "eval_set_stable": comparable,
            "lineage_recorded": config.round_index == 0 or config.parent_round_id is not None,
            "noise_floor_available": bool(floors_seq),
        }
        record = RoundRecord(
            round_index=config.round_index,
            run_id=config.run_id,
            parent_round_id=config.parent_round_id,
            eval_set_hash=eval_set_hash,
            engine=config.engine,
            type_scores=type_scores,
            deltas=deltas,
            cost=cost,
            escalation_count=escalation_count,
            created_at_ms=self._clock.now_ms(),
            gates=gates,
            verdict=round_verdict(assessments),
        )
        await self._trajectory.write_round_record(
            record, scored=scored, assessments=assessments, noise_floors=floors_seq
        )
        row = await self._trajectory.append_round(
            record,
            assessments=assessments,
            noise_floors=floors_seq,
            cost_basis=config.cost_basis,
            scored=scored,
            seed=config.seed,
            cap=config.default_cap,
            verify_every_step=config.verify_every_step,
        )
        logger.info(
            "research.round.finished",
            round_index=config.round_index,
            run_id=config.run_id,
            cells={
                f"{ts.problem_type.value}/{ts.split.value}": ts.mean_score for ts in type_scores
            },
            escalations=escalation_count,
            wall_clock_seconds=elapsed,
            operator_wait_seconds=self._operator_wait_seconds,
            verdict=record.verdict,
        )
        return RoundOutcome(
            record=record,
            attempts=outcomes,
            scored=scored,
            assessments=assessments,
            trajectory_row=row,
        )
