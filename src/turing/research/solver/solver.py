"""Loop 1: the within-project autonomous solver.

One project, one run, iterative refinement against the problem's own frozen
verifier. Build a solution, score it, revise, repeat until the verifier is
satisfied or the cap trips. Not parallel attempts, not tree search — those are
deliberately left for a future self-edit to propose, and a self-edit proposing
one would itself be a finding.

Three properties are what this module is actually for, and each one is an
invariant rather than a feature.

**The solver cannot quit — as a call-site guard on this writer.** There is no
path from "the solver decided" to a terminal state of its own choosing *in
this module*. :attr:`~turing.research.contracts.AttemptState.ABANDONED`
is written in exactly one method, :meth:`Solver._abandon`, which refuses to run
without an operator :class:`~turing.research.contracts.EscalationDecision`
carrying ``ABANDON``. That is a call-site guard, not a structural
impossibility: :meth:`~turing.research.contracts.Attempt.evolve` can still
reach ``ABANDONED`` with a leftover ``escalation_id``.
:attr:`~turing.research.contracts.AttemptState.FAILED_WITHIN_CAP`
is written in exactly one method, :meth:`Solver._fail_within_cap`, which
refuses to run unless the cap has actually tripped — a mechanical brake, not a
judgement. A project the solver judges hopeless raises an escalation and
suspends. Tests assert these placements against the parsed source, so adding a
second write site in this module fails the suite rather than review.

**The solver cannot exceed its cap.** Steps and tokens are charged in the same
transaction that journals the work that spent them, and checked before an
iteration starts. Wall-clock is charged the same way *and* enforced during an
iteration: every backend call, apply and verification runs under
``asyncio.wait_for`` with the attempt's remaining wall-clock as its timeout, so
a hung subprocess is cancelled when the budget expires rather than running
through the night. A ``TimeoutError`` charges the elapsed time, not a write-off
of whatever was left — a short model-call timeout is not the rest of the cap.
Apply and verify are the exception when that elapsed time is zero: they do
not charge a step, and a pending iteration plus leftover wall-clock skips
the cap gate, so a zero-elapsed timeout must escalate rather than retry.
The one bound the solver cannot enforce alone is the token count of a single
response — it does not exist until the response does — which is why
:attr:`~turing.research.solver.models.ProposalContext.remaining` is handed to
the backend, whose job it is to size the call.

**An interruption costs the remainder of an iteration, not the attempt.** See
:class:`~turing.research.solver.models.IterationPhase`.

**Loop 2 is not here.** Nothing self-modifies, nothing detects cheating,
nothing rolls back. The seam loop 2 attaches to is the round boundary above
this module, not inside it; the journal this solver writes
(:meth:`turing.research.solver.protocols.CheckpointStore.iterations`) is the
per-attempt half of the trajectory data a self-edit summary would later read,
filtered by :meth:`turing.research.contracts.RoundRecord.self_edit_visible_scores`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

from turing.research.contracts import (
    AttemptState,
    CapConsumption,
    CapDimension,
    ContractViolationError,
    EscalationProtocolError,
    EscalationReason,
    EscalationRequest,
    EscalationVerdict,
)
from turing.research.solver.errors import CheckpointError, SolverError
from turing.research.solver.models import (
    IterationPhase,
    IterationRecord,
    ProposalContext,
    SolverOutcome,
    SolverPolicy,
    better_result,
    summarise_progress,
)
from turing.research.solver.protocols import ResumableBackend, SystemClock

if TYPE_CHECKING:
    from collections.abc import Coroutine, Sequence

    from turing.research.contracts import (
        Attempt,
        EscalationDecision,
        Problem,
        VerificationResult,
    )
    from turing.research.solver.models import CommandOutcome, Proposal
    from turing.research.solver.protocols import (
        CheckpointStore,
        Clock,
        CommandRunner,
        EscalationChannel,
        ProposalBackend,
    )
    from turing.research.solver.workspace import Workspace, WorkspaceManager

__all__ = ["Solver"]

logger = structlog.get_logger("turing.research.solver")

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _Failure:
    """An iteration went wrong in a way only a human can resolve."""

    reason: EscalationReason
    summary: str


class Solver:
    """Runs one attempt at one problem until it passes, trips, or escalates.

    Every collaborator is injected, which is what lets the whole loop run in a
    test with no network, no API key and no waiting:

    Args:
        backend: Produces the next change. See
            :class:`~turing.research.solver.protocols.ProposalBackend` for why
            this is narrower than the model backend in
            :mod:`turing.research.backends`.
        store: Durable checkpoints and the iteration journal.
        workspaces: Creates and re-attaches attempt working directories.
        escalations: The operator gate — rich question out, three-valued
            answer back.
        policy: When the bar is met, and when to ask a human. Defaults to
            refine-until-cap with no target, which can never reach ``PASSED``;
            see :class:`~turing.research.solver.models.SolverPolicy`.
        clock: Injected so wall-clock accounting is testable.
        runner: Optional executor for commands a proposal asks for. Absent by
            default — running model-authored commands belongs to the safety
            layer and must be wired in deliberately.
    """

    def __init__(
        self,
        *,
        backend: ProposalBackend,
        store: CheckpointStore,
        workspaces: WorkspaceManager,
        escalations: EscalationChannel,
        policy: SolverPolicy | None = None,
        clock: Clock | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._backend = backend
        self._store = store
        self._workspaces = workspaces
        self._escalations = escalations
        self._policy = policy if policy is not None else SolverPolicy()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._runner = runner

    @property
    def policy(self) -> SolverPolicy:
        return self._policy

    # ----------------------------------------------------------------- #
    # Entry points
    # ----------------------------------------------------------------- #

    async def resume(
        self, attempt_id: str, problem: Problem, *, stop: asyncio.Event | None = None
    ) -> SolverOutcome:
        """Reload an attempt from its last checkpoint and carry on.

        This is the whole of resume from a caller's point of view: no replay,
        no reconstruction. Whatever was durable is what the run continues from.
        """
        stored = await self._store.load_attempt(attempt_id)
        if stored is None:
            raise CheckpointError(f"no checkpoint stored for attempt {attempt_id!r}")
        return await self.run(stored, problem, stop=stop)

    async def run(
        self, attempt: Attempt, problem: Problem, *, stop: asyncio.Event | None = None
    ) -> SolverOutcome:
        """Work ``attempt`` until it passes, trips its cap, or needs a human.

        Args:
            attempt: The checkpoint to start or continue from.
            problem: Its problem. The verifier is read from here and is never
                copied, wrapped, or handed to the backend.
            stop: Set it to ask for a clean pause at the next iteration
                boundary — the cooperative form of "the subscription window is
                closing". Cancellation also checkpoints, best effort.

        Returns:
            A :class:`~turing.research.solver.models.SolverOutcome`. Inspect
            ``terminal`` / ``suspended`` / ``paused`` to know whether to call
            again.
        """
        if attempt.problem_id != problem.id:
            raise ContractViolationError(
                f"attempt {attempt.attempt_id!r} is for problem {attempt.problem_id!r}, "
                f"not {problem.id!r}"
            )
        attempt = await self._reconcile(attempt)
        history = list(await self._store.iterations(attempt.attempt_id))
        escalation: EscalationRequest | None = None
        if attempt.is_terminal:
            return self._outcome(attempt, history, None)

        log = logger.bind(
            attempt_id=attempt.attempt_id,
            problem_id=problem.id,
            round_id=attempt.round_id,
        )
        # Reset on every call, so a resumed run may raise at most one extra
        # patience escalation before the counter re-arms. Bounded and visible
        # beats persisting a counter whose only job is to suppress a question.
        patience_floor = 0

        try:
            if attempt.state is AttemptState.ESCALATED:
                attempt, escalation = await self._resolve_pending(attempt)
                if attempt.state is not AttemptState.RUNNING:
                    return self._outcome(attempt, history, escalation)

            workspace = await self._prepare(attempt, problem)
            attempt = await self._start(attempt)
            log.info(
                "research.solver.started", state=attempt.state.value, cap=attempt.cap.max_steps
            )

            while True:
                if stop is not None and stop.is_set():
                    attempt = await self._pause(attempt)
                    break

                # An iteration left half-finished by an interruption is
                # already paid for — its step and tokens were charged in the
                # same transaction that journalled it. Finishing it costs
                # nothing more than the verification, and discarding it would
                # throw away spend the cap has already recorded. So the cap
                # gate guards *starting* work, not completing it. The one
                # exception is a spent wall-clock budget, where nothing can
                # run at all and skipping the gate would livelock.
                if not (_has_pending_iteration(history) and _wall_clock_remains(attempt)) and (
                    attempt.cap_exhausted
                ):
                    attempt, escalation = await self._on_cap_exhausted(attempt, problem, history)
                    if attempt.state is not AttemptState.RUNNING:
                        break
                    continue

                attempt, raised = await self._iterate(attempt, problem, workspace, history)
                escalation = raised if raised is not None else escalation
                if attempt.state is not AttemptState.RUNNING:
                    break

                progress = summarise_progress(_verified_results(history))
                if self._policy.satisfied_by(progress.best):
                    attempt = await self._mark_passed(attempt)
                    break

                attempt, raised, patience_floor = await self._check_patience(
                    attempt, problem, history, patience_floor
                )
                escalation = raised if raised is not None else escalation
                if attempt.state is not AttemptState.RUNNING:
                    break
        except asyncio.CancelledError:
            # Best effort. A single cancellation lets this write through, which
            # is what a closing window looks like in practice; the reliable
            # path is ``stop``, which checkpoints at an iteration boundary.
            with contextlib.suppress(BaseException):
                await asyncio.shield(asyncio.ensure_future(self._pause(attempt)))
            raise

        outcome = self._outcome(attempt, history, escalation)
        log.info(
            "research.solver.finished",
            state=outcome.state.value,
            iterations=outcome.iterations_completed,
            steps=attempt.consumed.steps,
            tokens=attempt.consumed.tokens,
        )
        return outcome

    async def propose_and_apply(
        self,
        context: ProposalContext,
        *,
        resume_token: str | None = None,
    ) -> Proposal:
        """One unit of forward progress for the round runner.

        Proposes via the backend and writes the edits. Does **not** verify,
        does **not** charge the cap, and does **not** take a
        :class:`~turing.research.contracts.Problem` — the runner owns those
        three, and handing the verifier in here would collapse the in-process
        isolation the runner is built around. :meth:`run` remains the
        standalone loop for tests that drive the solver without a runner.

        ``resume_token`` is restored onto the backend only when that
        backend's ledger is empty — a fresh instance after crash/restart.
        A live solver that already spent must not restore its own
        checkpoint over itself: ``UsageLedger.restore`` refuses a
        non-empty ledger, and forwarding the token on every runner step
        would kill the second successful call.
        """
        if (
            resume_token is not None
            and isinstance(self._backend, ResumableBackend)
            and _backend_ledger_is_fresh(self._backend)
        ):
            self._backend.restore(resume_token)
        proposal = await self._backend.propose(context)
        if proposal.no_viable_approach:
            return proposal
        workspace = self._workspaces.attach(context.workspace)
        try:
            await workspace.apply(proposal)
            if proposal.commands:
                await self._run_commands_unmetered(workspace, proposal, context)
        except Exception as exc:
            _carry_spent_tokens(exc, proposal.tokens)
            raise
        return proposal

    def backend_resume_token(self) -> str | None:
        """The wrapped backend's checkpoint, if it is resumable."""
        if isinstance(self._backend, ResumableBackend):
            return self._backend.checkpoint()
        return None

    async def _run_commands_unmetered(
        self,
        workspace: Workspace,
        proposal: Proposal,
        context: ProposalContext,
    ) -> tuple[CommandOutcome, ...]:
        """Run proposal commands without charging the attempt — the runner meters.

        Still refuses to run anything unless a :class:`CommandRunner` was
        wired in; that refusal is the safety-layer seam, not a cap concern.
        """
        if self._runner is None:
            raise SolverError(
                f"proposal {proposal.proposal_id!r} asked to run {len(proposal.commands)} "
                "command(s) but no CommandRunner is configured; executing model-authored "
                "commands belongs to the safety layer and is wired in deliberately"
            )
        timeout = max(0.0, context.remaining.wall_clock_seconds)
        outcomes: list[CommandOutcome] = []
        for command in proposal.commands:
            outcomes.append(
                await asyncio.wait_for(
                    self._runner.run(command, workspace=workspace.path, timeout_seconds=timeout),
                    timeout=timeout,
                )
            )
        return tuple(outcomes)

    # ----------------------------------------------------------------- #
    # One iteration: propose, apply, verify
    # ----------------------------------------------------------------- #

    async def _iterate(
        self,
        attempt: Attempt,
        problem: Problem,
        workspace: Workspace,
        history: list[IterationRecord],
    ) -> tuple[Attempt, EscalationRequest | None]:
        """Run — or finish — exactly one refinement iteration.

        The first thing it does is look at the journal. A trailing entry that
        is not ``VERIFIED`` is an iteration that was interrupted, and it is
        continued from that phase rather than restarted: no second backend
        call, no second token charge, and no re-application of a change that
        is already on disk.
        """
        pending = history[-1] if history else None
        if pending is not None and pending.phase is IterationPhase.VERIFIED:
            pending = None

        if pending is None:
            index = history[-1].iteration_index + 1 if history else 0
            attempt, record = await self._propose(attempt, problem, workspace, index, history)
            if record is None:
                return attempt, None  # wall-clock ran out mid-call; the cap gate takes it
            history.append(record)
        else:
            record = pending
            logger.info(
                "research.solver.iteration_resumed",
                attempt_id=attempt.attempt_id,
                iteration=record.iteration_index,
                phase=record.phase.value,
                proposal_id=record.proposal.proposal_id,
            )

        if record.proposal.no_viable_approach:
            record = record.advance(IterationPhase.VERIFIED, now_ms=self._clock.now_ms())
            history[-1] = record
            await self._store.commit_iteration(record, attempt)
            return await self._escalate(
                attempt,
                problem,
                EscalationReason.NO_VIABLE_APPROACH,
                f"the solver sees no viable approach after {len(history)} iteration(s): "
                f"{record.proposal.rationale}".strip(),
            )

        if record.phase is IterationPhase.PROPOSED:
            attempt, record, failure = await self._apply(attempt, workspace, record)
            history[-1] = record
            if failure is not None:
                return await self._escalate(attempt, problem, failure.reason, failure.summary)
            if record.phase is not IterationPhase.APPLIED:
                return attempt, None  # timed out; resume re-applies, the cap gate decides

        if record.phase is IterationPhase.APPLIED:
            attempt, record, failure = await self._verify(attempt, problem, workspace, record)
            history[-1] = record
            if failure is not None:
                return await self._escalate(attempt, problem, failure.reason, failure.summary)

        return attempt, None

    async def _propose(
        self,
        attempt: Attempt,
        problem: Problem,
        workspace: Workspace,
        index: int,
        history: Sequence[IterationRecord],
    ) -> tuple[Attempt, IterationRecord | None]:
        """Ask the backend for the next change and journal it before acting.

        Journalling before applying, in the same transaction as the token
        charge, is what makes an interruption cost the remainder of the
        iteration: the proposal is already durable, so the resumed run applies
        it instead of buying it again.
        """
        context = ProposalContext(
            problem_id=problem.id,
            problem_type=problem.problem_type,
            goal=problem.goal,
            score_scale=problem.verifier.score_scale,
            workspace=workspace.path,
            iteration_index=index,
            seed=attempt.seed,
            remaining=attempt.remaining,
            history=tuple(record.summarise() for record in history),
            last_result=_last_result(history),
            best_result=better_result(None, attempt.result),
        )
        started = self._clock.monotonic()
        try:
            proposal = await self._deadline(attempt, self._backend.propose(context))
        except TimeoutError:
            attempt = await self._charge(attempt, started, steps=1)
            logger.warning(
                "research.solver.propose_timed_out",
                attempt_id=attempt.attempt_id,
                iteration=index,
            )
            return attempt, None

        elapsed = self._elapsed(started)
        now = self._clock.now_ms()
        record = IterationRecord(
            attempt_id=attempt.attempt_id,
            iteration_index=index,
            phase=IterationPhase.PROPOSED,
            proposal=proposal,
            started_at_ms=now,
            updated_at_ms=now,
            wall_clock_seconds=elapsed,
        )
        attempt = attempt.record_consumption(
            CapConsumption(steps=1, tokens=proposal.tokens, wall_clock_seconds=elapsed),
            now_ms=now,
        )
        # The backend's own resumable state rides along in the same commit as
        # the tokens it just spent, so a reloaded attempt points at the
        # conversation those tokens bought rather than a fresh one.
        attempt = attempt.evolve(
            now_ms=now, step_index=index + 1, resume_token=self._backend_token(attempt)
        )
        await self._store.commit_iteration(record, attempt)
        logger.info(
            "research.solver.proposed",
            attempt_id=attempt.attempt_id,
            iteration=index,
            proposal_id=proposal.proposal_id,
            digest=proposal.digest(),
            tokens=proposal.tokens,
            files=len(proposal.edits),
        )
        return attempt, record

    async def _apply(
        self, attempt: Attempt, workspace: Workspace, record: IterationRecord
    ) -> tuple[Attempt, IterationRecord, _Failure | None]:
        """Write the proposal into the workspace, then run any commands."""
        started = self._clock.monotonic()
        try:
            applied = await self._deadline(attempt, workspace.apply(record.proposal))
            outcomes = await self._run_commands(attempt, workspace, record.proposal)
        except TimeoutError:
            return await self._charge_timeout(
                attempt,
                started,
                record,
                what=f"applying proposal {record.proposal.proposal_id!r}",
            )
        except (SolverError, OSError) as exc:
            attempt = await self._charge(attempt, started)
            return (
                attempt,
                record,
                _Failure(
                    EscalationReason.HARNESS_FAILURE,
                    f"applying proposal {record.proposal.proposal_id!r} failed: "
                    f"{type(exc).__name__}: {exc}",
                ),
            )

        elapsed = self._elapsed(started)
        now = self._clock.now_ms()
        record = record.advance(
            IterationPhase.APPLIED,
            now_ms=now,
            applied_paths=applied,
            command_outcomes=outcomes,
            wall_clock_seconds=record.wall_clock_seconds + elapsed,
        )
        attempt = attempt.record_consumption(CapConsumption(wall_clock_seconds=elapsed), now_ms=now)
        await self._store.commit_iteration(record, attempt)
        return attempt, record, None

    async def _run_commands(
        self, attempt: Attempt, workspace: Workspace, proposal: Proposal
    ) -> tuple[CommandOutcome, ...]:
        if not proposal.commands:
            return ()
        if self._runner is None:
            raise SolverError(
                f"proposal {proposal.proposal_id!r} asked to run {len(proposal.commands)} "
                "command(s) but no CommandRunner is configured; executing model-authored "
                "commands belongs to the safety layer and is wired in deliberately"
            )
        outcomes: list[CommandOutcome] = []
        for command in proposal.commands:
            outcomes.append(
                await self._deadline(
                    attempt,
                    self._runner.run(
                        command,
                        workspace=workspace.path,
                        timeout_seconds=attempt.remaining.wall_clock_seconds,
                    ),
                )
            )
        return tuple(outcomes)

    async def _verify(
        self,
        attempt: Attempt,
        problem: Problem,
        workspace: Workspace,
        record: IterationRecord,
    ) -> tuple[Attempt, IterationRecord, _Failure | None]:
        """Grade the workspace against the problem's frozen verifier.

        The verifier is called, never constructed, copied or configured here.
        It is handed the workspace *path* and nothing else, and its answer is
        checked for provenance and instrument integrity: a result claiming a
        different problem or verifier, or one with
        :attr:`~turing.research.contracts.VerificationResult.harness_failed`
        set, goes to a human rather than into the trajectory.
        """
        now = self._clock.now_ms()
        # VERIFYING is in-flight, not a journal phase. Persisting it made a
        # crash during verify unresumable (``is_resumable`` used to exclude
        # it) and lost the rest of the attempt. The durable phase is APPLIED;
        # resume re-runs verify from there. In-memory VERIFYING still marks
        # the call for logs and cooperative pause.
        attempt = attempt.evolve(now_ms=now, state=AttemptState.VERIFYING)

        started = self._clock.monotonic()
        try:
            result = await self._deadline(attempt, problem.verifier.verify(workspace.path))
        except TimeoutError:
            return await self._charge_timeout(
                attempt,
                started,
                record,
                state=AttemptState.RUNNING,
                what=f"verifying iteration {record.iteration_index}",
            )
        except Exception as exc:
            # Deliberately broad: a verifier is arbitrary operator-supplied
            # code running real subprocesses, and every way it can fail is a
            # question for a human rather than something to classify here.
            attempt = await self._charge(attempt, started, state=AttemptState.RUNNING)
            return (
                attempt,
                record,
                _Failure(
                    EscalationReason.VERIFIER_UNRUNNABLE,
                    f"verifier {problem.verifier_id!r} raised {type(exc).__name__}: {exc}",
                ),
            )

        elapsed = self._elapsed(started)
        now = self._clock.now_ms()
        if result.problem_id != problem.id or result.verifier_id != problem.verifier_id:
            attempt = await self._charge(attempt, started, state=AttemptState.RUNNING)
            return (
                attempt,
                record,
                _Failure(
                    EscalationReason.HARNESS_FAILURE,
                    f"verifier returned a result for {result.problem_id!r}/"
                    f"{result.verifier_id!r} while grading {problem.id!r}/"
                    f"{problem.verifier_id!r}",
                ),
            )

        if result.harness_failed:
            attempt = await self._charge(attempt, started, state=AttemptState.RUNNING)
            logger.warning(
                "research.solver.harness_failure",
                attempt_id=attempt.attempt_id,
                detail=result.detail,
            )
            return (
                attempt,
                record,
                _Failure(
                    EscalationReason.HARNESS_FAILURE,
                    result.detail or "verifier reported a harness failure",
                ),
            )

        record = record.advance(
            IterationPhase.VERIFIED,
            now_ms=now,
            result=result,
            wall_clock_seconds=record.wall_clock_seconds + elapsed,
        )
        best = better_result(attempt.result, result)
        attempt = attempt.record_consumption(CapConsumption(wall_clock_seconds=elapsed), now_ms=now)
        attempt = attempt.evolve(
            now_ms=now,
            state=AttemptState.RUNNING,
            result=best,
        )
        await self._store.commit_iteration(record, attempt)
        logger.info(
            "research.solver.verified",
            attempt_id=attempt.attempt_id,
            iteration=record.iteration_index,
            score=result.score,
            score_scale=result.score_scale,
            passed_correctness=result.passed_correctness,
            best_score=None if best is None else best.score,
        )
        return attempt, record, None

    # ----------------------------------------------------------------- #
    # Cap
    # ----------------------------------------------------------------- #

    async def _deadline(self, attempt: Attempt, awaitable: Coroutine[Any, Any, T]) -> T:
        """Run ``awaitable`` under the attempt's remaining wall-clock budget.

        This is the difference between a cap that is checked and a cap that is
        enforced. Without it a single hung subprocess — a training run that
        never converges, a verifier waiting on a lock — burns the whole night
        and the cap only notices afterwards.
        """
        return await asyncio.wait_for(
            awaitable, timeout=max(0.0, attempt.remaining.wall_clock_seconds)
        )

    def _elapsed(self, started: float) -> float:
        return max(0.0, self._clock.monotonic() - started)

    async def _charge(
        self,
        attempt: Attempt,
        started: float,
        *,
        steps: int = 0,
        state: AttemptState | None = None,
    ) -> Attempt:
        now = self._clock.now_ms()
        attempt = attempt.record_consumption(
            CapConsumption(steps=steps, wall_clock_seconds=self._elapsed(started)), now_ms=now
        )
        if state is not None:
            attempt = attempt.evolve(now_ms=now, state=state)
        await self._store.save_attempt(attempt)
        return attempt

    async def _charge_timeout(
        self,
        attempt: Attempt,
        started: float,
        record: IterationRecord,
        *,
        what: str,
        state: AttemptState | None = None,
    ) -> tuple[Attempt, IterationRecord, _Failure | None]:
        """Charge a timed-out apply/verify, or escalate if that charge is a no-op.

        Propose already charges a step on ``TimeoutError``, so a zero-elapsed
        model timeout still moves the cap. Apply and verify charge only
        wall-clock. Combined with the pending-iteration gate — which skips
        the cap check while wall-clock remains — a zero-elapsed timeout
        would otherwise retry forever.
        """
        elapsed = self._elapsed(started)
        attempt = await self._charge(attempt, started, state=state)
        if elapsed == 0.0:
            return (
                attempt,
                record,
                _Failure(
                    EscalationReason.HARNESS_FAILURE,
                    f"{what} timed out with no elapsed time; retrying would livelock",
                ),
            )
        return attempt, record, None

    async def _on_cap_exhausted(
        self, attempt: Attempt, problem: Problem, history: Sequence[IterationRecord]
    ) -> tuple[Attempt, EscalationRequest | None]:
        dimensions = ", ".join(d.value for d in attempt.exceeded_cap_dimensions)
        if not self._policy.escalate_on_cap_exhausted:
            return await self._fail_within_cap(attempt), None
        return await self._escalate(
            attempt,
            problem,
            EscalationReason.CAP_EXHAUSTED,
            f"cap exhausted on {dimensions} after {len(history)} iteration(s); "
            f"steps={attempt.consumed.steps}/{attempt.cap.max_steps} "
            f"tokens={attempt.consumed.tokens}/{attempt.cap.max_tokens} "
            f"wall_clock={attempt.consumed.wall_clock_seconds:.1f}/"
            f"{attempt.cap.max_wall_clock_seconds:.1f}s",
        )

    async def _check_patience(
        self,
        attempt: Attempt,
        problem: Problem,
        history: Sequence[IterationRecord],
        patience_floor: int,
    ) -> tuple[Attempt, EscalationRequest | None, int]:
        """Ask a human when refinement stops paying, rather than giving up.

        Both branches escalate. Neither terminates: "this looks hopeless" is
        exactly the judgement the design forbids the solver from acting on.
        """
        progress = summarise_progress(_verified_results(history))
        if progress.verified_iterations <= patience_floor:
            return attempt, None, patience_floor

        regression_patience = self._policy.regression_patience
        if (
            regression_patience is not None
            and progress.consecutive_regressions >= regression_patience
        ):
            attempt, raised = await self._escalate(
                attempt,
                problem,
                EscalationReason.REPEATED_REGRESSION,
                f"{progress.consecutive_regressions} consecutive iterations scored worse "
                f"than the one before; best so far is "
                f"{None if progress.best is None else progress.best.score}",
            )
            return attempt, raised, progress.verified_iterations

        patience = self._policy.patience
        if patience is not None and progress.stagnant_iterations >= patience:
            attempt, raised = await self._escalate(
                attempt,
                problem,
                EscalationReason.NO_VIABLE_APPROACH,
                f"{progress.stagnant_iterations} iterations without improving on "
                f"{None if progress.best is None else progress.best.score}",
            )
            return attempt, raised, progress.verified_iterations

        return attempt, None, patience_floor

    # ----------------------------------------------------------------- #
    # Escalation — the only route out of a run the solver did not finish
    # ----------------------------------------------------------------- #

    async def _escalate(
        self,
        attempt: Attempt,
        problem: Problem,
        reason: EscalationReason,
        summary: str,
    ) -> tuple[Attempt, EscalationRequest]:
        """Ask the operator, checkpoint, and either continue or suspend.

        Information flows outward richly and comes back as one of three
        words. If no decision is waiting, the attempt stays ``ESCALATED`` —
        neither terminal nor resumable — and the run returns. That is the
        unattended case and the normal one.
        """
        now = self._clock.now_ms()
        request = EscalationRequest(
            request_id=f"esc-{attempt.attempt_id}-{attempt.checkpoint_seq}",
            problem_id=problem.id,
            attempt_id=attempt.attempt_id,
            round_id=attempt.round_id,
            reason=reason,
            summary=summary,
            cap=attempt.cap,
            consumed=attempt.consumed,
            created_at_ms=now,
            best_result=better_result(None, attempt.result),
        )
        attempt = attempt.evolve(
            now_ms=now, state=AttemptState.ESCALATED, escalation_id=request.request_id
        )
        await self._store.save_escalation(request)
        await self._store.save_attempt(attempt)
        logger.warning(
            "research.solver.escalated",
            attempt_id=attempt.attempt_id,
            problem_id=problem.id,
            request_id=request.request_id,
            reason=reason.value,
            summary=summary,
        )
        await self._escalations.raise_escalation(request)

        decision = await self._escalations.await_decision(request.request_id)
        if decision is None:
            return attempt, request
        return await self._apply_decision(attempt, decision), request

    async def _resolve_pending(self, attempt: Attempt) -> tuple[Attempt, EscalationRequest | None]:
        """Re-poll an escalation raised by an earlier run of this attempt."""
        request_id = attempt.escalation_id
        if request_id is None:
            raise ContractViolationError(
                f"attempt {attempt.attempt_id!r} is ESCALATED with no escalation id; "
                "only an operator decision moves it and there is nothing to answer"
            )
        request = await self._store.load_escalation(request_id)
        decision = await self._escalations.await_decision(request_id)
        if decision is None:
            return attempt, request
        return await self._apply_decision(attempt, decision), request

    async def _apply_decision(self, attempt: Attempt, decision: EscalationDecision) -> Attempt:
        """Turn one of the three operator words into the next attempt state.

        ``CONTINUE`` on an attempt whose cap has already tripped resolves to
        ``FAILED_WITHIN_CAP``: continuing without budget is not a thing that
        can be done, and inventing budget the operator did not grant would
        defeat the brake. The operator who wants more work says
        ``extend_cap``.
        """
        if decision.request_id != attempt.escalation_id:
            raise EscalationProtocolError(
                f"decision answers {decision.request_id!r} but attempt "
                f"{attempt.attempt_id!r} is waiting on {attempt.escalation_id!r}"
            )
        if decision.verdict is EscalationVerdict.ABANDON:
            return await self._abandon(attempt, decision)

        now = self._clock.now_ms()
        if decision.verdict is EscalationVerdict.EXTEND_CAP:
            assert decision.cap_extension is not None  # guaranteed by EscalationDecision
            attempt = attempt.apply_cap_extension(decision.cap_extension, now_ms=now)
        attempt = attempt.evolve(
            now_ms=now,
            state=AttemptState.RUNNING,
            escalation_id=None,
        )
        if attempt.cap_exhausted:
            return await self._fail_within_cap(attempt)
        await self._store.save_attempt(attempt)
        logger.info(
            "research.solver.escalation_resolved",
            attempt_id=attempt.attempt_id,
            verdict=decision.verdict.value,
            max_steps=attempt.cap.max_steps,
            extensions=attempt.cap.extension_count,
        )
        return attempt

    async def _abandon(self, attempt: Attempt, decision: EscalationDecision) -> Attempt:
        """The only write of ``ABANDONED`` in this module; it checks the verdict.

        This is a call-site guard, not a structural impossibility.
        :meth:`~turing.research.contracts.Attempt.evolve` reaches
        ``ABANDONED`` with a leftover ``escalation_id`` and no
        :class:`EscalationDecision`. Here the method refuses any verdict but
        ``ABANDON``, so *this writer* cannot abandon without an operator
        decision object in hand. Other writers, or a future ``evolve`` call,
        are not bound by this guard.
        """
        if decision.verdict is not EscalationVerdict.ABANDON:
            raise EscalationProtocolError(
                f"ABANDONED is reachable only from an operator ABANDON verdict, "
                f"not {decision.verdict.value}"
            )
        attempt = attempt.evolve(
            now_ms=self._clock.now_ms(),
            state=AttemptState.ABANDONED,
            escalation_id=decision.request_id,
        )
        await self._store.save_attempt(attempt)
        logger.warning(
            "research.solver.abandoned",
            attempt_id=attempt.attempt_id,
            request_id=decision.request_id,
        )
        return attempt

    async def _fail_within_cap(self, attempt: Attempt) -> Attempt:
        """The only write of ``FAILED_WITHIN_CAP``, and it needs a tripped cap.

        "Failed within cap" is a first-class outcome — without it a project
        that never terminates is neither a pass nor a fail and solve rate is
        undefined. It is a mechanical brake, not a judgement, so the guard
        insists the brake actually engaged.
        """
        if not attempt.cap_exhausted:
            raise SolverError(
                f"attempt {attempt.attempt_id!r} has budget left "
                f"({attempt.remaining}); FAILED_WITHIN_CAP requires a tripped cap and "
                "the solver has no self-quit transition"
            )
        attempt = attempt.evolve(now_ms=self._clock.now_ms(), state=AttemptState.FAILED_WITHIN_CAP)
        await self._store.save_attempt(attempt)
        logger.info(
            "research.solver.failed_within_cap",
            attempt_id=attempt.attempt_id,
            dimensions=[d.value for d in attempt.exceeded_cap_dimensions],
            best_score=None if attempt.result is None else attempt.result.score,
        )
        return attempt

    async def _mark_passed(self, attempt: Attempt) -> Attempt:
        """The only write of ``PASSED``, and it needs the frozen bar to be met."""
        if not self._policy.satisfied_by(attempt.result):
            raise SolverError(
                f"attempt {attempt.attempt_id!r} does not meet the verifier's bar; "
                "PASSED is the verifier's word, not the solver's"
            )
        attempt = attempt.evolve(now_ms=self._clock.now_ms(), state=AttemptState.PASSED)
        await self._store.save_attempt(attempt)
        logger.info(
            "research.solver.passed",
            attempt_id=attempt.attempt_id,
            score=None if attempt.result is None else attempt.result.score,
            steps=attempt.consumed.steps,
        )
        return attempt

    # ----------------------------------------------------------------- #
    # Lifecycle plumbing
    # ----------------------------------------------------------------- #

    async def _reconcile(self, attempt: Attempt) -> Attempt:
        """Refuse to run behind a newer checkpoint for the same attempt."""
        stored = await self._store.load_attempt(attempt.attempt_id)
        if stored is None:
            await self._store.save_attempt(attempt)
            return attempt
        if stored.checkpoint_seq > attempt.checkpoint_seq:
            raise CheckpointError(
                f"attempt {attempt.attempt_id!r} has a newer checkpoint "
                f"({stored.checkpoint_seq} > {attempt.checkpoint_seq}); reload it or call "
                "Solver.resume(), rather than rewinding another process's work"
            )
        return attempt

    async def _prepare(self, attempt: Attempt, problem: Problem) -> Workspace:
        return await self._workspaces.prepare(problem, attempt.workspace_path)

    def _backend_token(self, attempt: Attempt) -> str | None:
        """The backend's resume token, if it has one to give."""
        if isinstance(self._backend, ResumableBackend):
            return self._backend.checkpoint()
        return attempt.resume_token

    async def _start(self, attempt: Attempt) -> Attempt:
        if not attempt.is_resumable:
            raise ContractViolationError(f"cannot start an attempt in state {attempt.state.value}")
        if (
            attempt.resume_token is not None
            and isinstance(self._backend, ResumableBackend)
            and _backend_ledger_is_fresh(self._backend)
        ):
            self._backend.restore(attempt.resume_token)
            logger.info(
                "research.solver.backend_restored",
                attempt_id=attempt.attempt_id,
            )
        now = self._clock.now_ms()
        attempt = attempt.resume(now_ms=now)
        if attempt.started_at_ms is None:
            attempt = attempt.evolve(now_ms=now, started_at_ms=now)
        await self._store.save_attempt(attempt)
        return attempt

    async def _pause(self, attempt: Attempt) -> Attempt:
        """Checkpoint an interrupted attempt so a later process resumes it."""
        if attempt.is_terminal or attempt.state is AttemptState.ESCALATED:
            return attempt
        attempt = attempt.pause(
            now_ms=self._clock.now_ms(), resume_token=self._backend_token(attempt)
        )
        await self._store.save_attempt(attempt)
        logger.info(
            "research.solver.paused",
            attempt_id=attempt.attempt_id,
            steps=attempt.consumed.steps,
            tokens=attempt.consumed.tokens,
        )
        return attempt

    def _outcome(
        self,
        attempt: Attempt,
        history: Sequence[IterationRecord],
        escalation: EscalationRequest | None,
    ) -> SolverOutcome:
        return SolverOutcome(
            attempt=attempt,
            iterations_completed=sum(
                1 for record in history if record.phase is IterationPhase.VERIFIED
            ),
            best_result=better_result(None, attempt.result),
            escalation=escalation,
        )


def _backend_ledger_is_fresh(backend: object) -> bool:
    """True when ``restore`` would not discard spend already on this instance.

    The runner-facing backend is often a wrapper (``ProposalAdapter``) whose
    ledger lives on the inner engine. Test doubles without a ledger are
    treated as fresh so existing restore behaviour is unchanged.
    """
    ledger = getattr(backend, "ledger", None)
    if ledger is None:
        inner = getattr(backend, "backend", None)
        ledger = getattr(inner, "ledger", None) if inner is not None else None
    if ledger is None:
        return True
    return not (
        getattr(ledger, "steps", 0)
        or getattr(ledger, "tokens", 0)
        or getattr(ledger, "wall_clock_seconds", 0)
        or getattr(ledger, "provider_calls", 0)
    )


def _carry_spent_tokens(exc: BaseException, tokens: int) -> None:
    """Copy accounted spend onto ``exc`` so the runner can charge it.

    Apply/command can raise after ``generate`` already recorded usage.
    Leaving that figure only on the backend ledger lets operator CONTINUE
    retry with the spend omitted. Tokens here are the backend's accounted
    total for the call, never a model-authored JSON field.
    """
    if tokens <= 0:
        return
    if getattr(exc, "usage", None) is not None:
        return
    current = getattr(exc, "tokens", None)
    if isinstance(current, int) and current > 0:
        return
    with contextlib.suppress(AttributeError, TypeError):
        exc.tokens = tokens  # type: ignore[attr-defined]


def _has_pending_iteration(history: Sequence[IterationRecord]) -> bool:
    """True when the journal's tail is an iteration that never closed."""
    return bool(history) and history[-1].phase is not IterationPhase.VERIFIED


def _wall_clock_remains(attempt: Attempt) -> bool:
    return CapDimension.WALL_CLOCK not in attempt.exceeded_cap_dimensions


def _verified_results(history: Sequence[IterationRecord]) -> tuple[VerificationResult, ...]:
    return tuple(
        result
        for record in history
        if (result := record.result) is not None and not result.harness_failed
    )


def _last_result(history: Sequence[IterationRecord]) -> VerificationResult | None:
    results = _verified_results(history)
    return results[-1] if results else None
