"""Checkpoint and resume.

The requirement: the operator's Claude subscription closes at unpredictable
points, and an interruption must cost the remainder of an iteration rather
than the attempt. The two ways to get that wrong are double-counting consumed
budget and re-applying a change that already landed, so every test here
measures one of those directly rather than asserting that the run "worked".

Interruptions are simulated by a store that dies immediately *after* a commit,
which is the honest failure: the checkpoint is durable and the process is gone.
The last test does it for real — a file-backed SQLite database, closed and
reopened by a second solver with its own backend.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import AttemptState, Cap, ContractViolationError, Problem
from turing.research.solver import (
    FileEdit,
    InMemoryCheckpointStore,
    IterationPhase,
    Proposal,
    Solver,
    SolverPolicy,
    SqliteCheckpointStore,
)

from .conftest import (
    SOLUTION_FILE,
    CrashAfterStore,
    FakeClock,
    LadderBackend,
    NumberVerifier,
    ScriptedBackend,
    ScriptedChannel,
    SimulatedCrashError,
    fresh_with_cap,
)

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import Attempt
    from turing.research.solver import WorkspaceManager

SENTINEL = "written after the crash"


def _proposals(values: list[float], tokens: int = 100) -> list[Proposal]:
    return [
        Proposal(
            proposal_id=f"p{i}",
            rationale=f"try {value}",
            edits=(FileEdit(relative_path=SOLUTION_FILE, content=str(value)),),
            tokens=tokens,
        )
        for i, value in enumerate(values)
    ]


def _solver(
    backend: object,
    store: object,
    manager: object,
    channel: object,
    clock: object,
    policy: SolverPolicy | None = None,
) -> Solver:
    return Solver(
        backend=backend,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        workspaces=manager,  # type: ignore[arg-type]
        escalations=channel,  # type: ignore[arg-type]
        policy=policy if policy is not None else SolverPolicy(),
        clock=clock,  # type: ignore[arg-type]
    )


async def _crash_at(
    commits: int,
    attempt: Attempt,
    problem: Problem,
    store: InMemoryCheckpointStore,
    manager: WorkspaceManager,
    clock: FakeClock,
    proposals: list[Proposal],
) -> ScriptedBackend:
    """Run until the given commit lands, then die. Returns the used backend."""
    backend = ScriptedBackend(proposals)
    crashing = CrashAfterStore(store, crash_after_commits=commits)
    with pytest.raises(SimulatedCrashError):
        await _solver(backend, crashing, manager, ScriptedChannel(), clock).run(attempt, problem)
    return backend


class TestResumeAtEachPhase:
    async def test_crash_before_any_work_loses_nothing(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = ScriptedBackend([])  # exhausted immediately
        with pytest.raises(AssertionError):
            await _solver(backend, store, manager, ScriptedChannel(), clock).run(attempt, problem)

        resumed = ScriptedBackend(_proposals([4.0]))
        outcome = await _solver(
            resumed, store, manager, ScriptedChannel(), clock, SolverPolicy(target_score=4.0)
        ).resume(attempt.attempt_id, problem)

        assert outcome.passed
        assert outcome.attempt.consumed.steps == 1

    async def test_crash_after_propose_replays_the_journalled_change(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Interrupted before the write landed, so resume must finish applying.

        The sentinel makes the difference observable: if the resumed run had
        skipped the apply, the verifier would read the sentinel and score
        zero. It reads 4.0, so the journalled proposal was re-applied — from
        the journal, without asking the model again.
        """
        await _crash_at(1, attempt, problem, store, manager, clock, _proposals([4.0]))

        pending = await store.latest_iteration(attempt.attempt_id)
        assert pending is not None
        assert pending.phase is IterationPhase.PROPOSED
        (attempt.workspace_path / SOLUTION_FILE).write_text(SENTINEL, encoding="utf-8")

        resumed_backend = ScriptedBackend(_proposals([1.0, 2.0]))
        outcome = await _solver(
            resumed_backend,
            store,
            manager,
            ScriptedChannel(),
            clock,
            SolverPolicy(target_score=4.0),
        ).resume(attempt.attempt_id, problem)

        assert outcome.passed
        assert outcome.best_result is not None
        assert outcome.best_result.score == 4.0
        assert (attempt.workspace_path / SOLUTION_FILE).read_text(encoding="utf-8") == "4.0"

    async def test_crash_after_propose_does_not_call_the_backend_again(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """The tokens for iteration 0 were charged; they must not be charged twice."""
        await _crash_at(1, attempt, problem, store, manager, clock, _proposals([4.0]))

        resumed_backend = ScriptedBackend(_proposals([1.0, 2.0]))
        outcome = await _solver(
            resumed_backend,
            store,
            manager,
            ScriptedChannel(),
            clock,
            SolverPolicy(target_score=4.0),
        ).resume(attempt.attempt_id, problem)

        assert resumed_backend.calls == 0
        assert outcome.attempt.consumed.steps == 1
        assert outcome.attempt.consumed.tokens == 100

    async def test_crash_after_apply_does_not_re_apply(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Mirror image of the previous test, and the sharper of the two.

        The change is already on disk, so resume must verify and nothing else.
        Overwriting the file with a sentinel and finding it *still there*
        after the resumed run is direct evidence the apply was not repeated.
        """
        capped = fresh_with_cap(
            attempt, Cap(max_steps=1, max_tokens=10_000, max_wall_clock_seconds=3600.0)
        )
        await _crash_at(2, capped, problem, store, manager, clock, _proposals([4.0]))

        pending = await store.latest_iteration(attempt.attempt_id)
        assert pending is not None
        assert pending.phase is IterationPhase.APPLIED
        assert (attempt.workspace_path / SOLUTION_FILE).read_text(encoding="utf-8") == "4.0"
        (attempt.workspace_path / SOLUTION_FILE).write_text(SENTINEL, encoding="utf-8")

        resumed_backend = ScriptedBackend(_proposals([9.0]))
        attempt_capped = await store.load_attempt(attempt.attempt_id)
        assert attempt_capped is not None
        outcome = await _solver(resumed_backend, store, manager, ScriptedChannel(), clock).run(
            attempt_capped,
            problem,
        )

        assert (attempt.workspace_path / SOLUTION_FILE).read_text(encoding="utf-8") == SENTINEL
        assert resumed_backend.calls == 0
        records = await store.iterations(attempt.attempt_id)
        assert records[0].result is not None
        assert records[0].result.passed_correctness is False  # it verified the sentinel
        assert outcome.attempt.consumed.steps == 1

    async def test_crash_during_verify_leaves_a_resumable_checkpoint(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """VERIFYING must not be the last durable state of a killed process."""

        class ProcessDied(BaseException):
            """The process is gone — not a verifier exception the solver classifies."""

        @dataclass(frozen=True)
        class DyingVerifier(NumberVerifier):
            async def verify(self, workspace):  # type: ignore[no-untyped-def]
                raise ProcessDied("killed during verify")

        dying = Problem(
            id=problem.id,
            problem_type=problem.problem_type,
            goal=problem.goal,
            workspace_template=problem.workspace_template,
            verifier=DyingVerifier(
                verifier_id=problem.verifier.verifier_id,
                problem_id=problem.id,
                description=problem.verifier.description,
                score_scale=problem.verifier.score_scale,
            ),
            split=problem.split,
        )
        with pytest.raises(ProcessDied):
            await _solver(
                ScriptedBackend(_proposals([4.0])),
                store,
                manager,
                ScriptedChannel(),
                clock,
            ).run(attempt, dying)

        stored = await store.load_attempt(attempt.attempt_id)
        assert stored is not None
        assert stored.state is not AttemptState.VERIFYING
        assert stored.is_resumable
        pending = await store.latest_iteration(attempt.attempt_id)
        assert pending is not None
        assert pending.phase is IterationPhase.APPLIED

        outcome = await _solver(
            ScriptedBackend(_proposals([9.0])),
            store,
            manager,
            ScriptedChannel(),
            clock,
            SolverPolicy(target_score=4.0),
        ).resume(attempt.attempt_id, problem)
        assert outcome.passed
        assert outcome.best_result is not None
        assert outcome.best_result.score == 4.0

    async def test_a_leftover_verifying_checkpoint_resumes(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Even a VERIFYING row already on disk must not brick the attempt."""
        await _crash_at(2, attempt, problem, store, manager, clock, _proposals([4.0]))
        stored = await store.load_attempt(attempt.attempt_id)
        assert stored is not None
        await store.save_attempt(stored.evolve(now_ms=clock.now_ms(), state=AttemptState.VERIFYING))
        stuck = await store.load_attempt(attempt.attempt_id)
        assert stuck is not None
        assert stuck.state is AttemptState.VERIFYING

        outcome = await _solver(
            ScriptedBackend(_proposals([9.0])),
            store,
            manager,
            ScriptedChannel(),
            clock,
            SolverPolicy(target_score=4.0),
        ).resume(attempt.attempt_id, problem)
        assert outcome.passed
        assert outcome.best_result is not None
        assert outcome.best_result.score == 4.0

    async def test_a_half_paid_iteration_finishes_even_when_the_cap_has_tripped(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """The step was charged before the crash; the score must not be lost.

        The cap gate guards *starting* work, not finishing what is paid for.
        Otherwise a crash on the last permitted iteration would burn a step
        and record nothing.
        """
        capped = fresh_with_cap(
            attempt, Cap(max_steps=1, max_tokens=10_000, max_wall_clock_seconds=3600.0)
        )
        await _crash_at(1, capped, problem, store, manager, clock, _proposals([4.0]))

        channel = ScriptedChannel()
        outcome = await _solver(ScriptedBackend([]), store, manager, channel, clock).resume(
            attempt.attempt_id, problem
        )

        assert outcome.iterations_completed == 1
        assert outcome.best_result is not None
        assert outcome.best_result.score == 4.0
        assert outcome.suspended  # then the cap trips, and it asks

    async def test_crash_after_verify_starts_the_next_iteration(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        await _crash_at(3, attempt, problem, store, manager, clock, _proposals([1.0, 5.0]))

        pending = await store.latest_iteration(attempt.attempt_id)
        assert pending is not None
        assert pending.phase is IterationPhase.VERIFIED

        resumed_backend = ScriptedBackend(_proposals([1.0, 5.0])[1:])
        outcome = await _solver(
            resumed_backend,
            store,
            manager,
            ScriptedChannel(),
            clock,
            SolverPolicy(target_score=5.0),
        ).resume(attempt.attempt_id, problem)

        assert resumed_backend.calls == 1
        assert resumed_backend.contexts[0].iteration_index == 1
        assert outcome.passed
        assert outcome.attempt.consumed.steps == 2
        assert outcome.attempt.consumed.tokens == 200


class TestBudgetIsChargedExactlyOnce:
    @pytest.mark.parametrize("crash_after", [1, 2, 3])
    async def test_an_interruption_never_double_charges(
        self,
        crash_after: int,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Whatever phase it died in, the totals match an uninterrupted run.

        This is the property the whole checkpoint design exists for. If the
        commit of a journal entry and the budget it charged could ever come
        apart, one of these three parameters would be off by one iteration's
        tokens.
        """
        await _crash_at(
            crash_after, attempt, problem, store, manager, clock, _proposals([1.0, 2.0, 3.0])
        )

        # Iteration 0 is journalled in all three cases, so a correct resume
        # asks the backend only for iterations 1 and 2 whichever phase it died
        # in. A backend script this short is itself an assertion: an extra
        # call would exhaust it and fail.
        resumed_backend = ScriptedBackend(_proposals([1.0, 2.0, 3.0])[1:])
        outcome = await _solver(
            resumed_backend,
            store,
            manager,
            ScriptedChannel(),
            clock,
            SolverPolicy(target_score=3.0),
        ).resume(attempt.attempt_id, problem)

        assert outcome.passed
        assert resumed_backend.calls == 2
        assert outcome.attempt.consumed.steps == 3
        assert outcome.attempt.consumed.tokens == 300
        assert outcome.iterations_completed == 3


class TestCooperativePause:
    async def test_a_stop_event_pauses_at_an_iteration_boundary(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """The reliable interruption path: the window is about to close."""
        stop = asyncio.Event()
        backend = LadderBackend(hook=lambda ctx: stop.set() if ctx.iteration_index == 1 else None)

        outcome = await _solver(backend, store, manager, ScriptedChannel(), clock).run(
            attempt, problem, stop=stop
        )

        assert outcome.paused
        assert outcome.state is AttemptState.PAUSED
        assert outcome.attempt.is_resumable
        assert backend.calls == 2

    async def test_a_paused_attempt_resumes_where_it_stopped(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        stop = asyncio.Event()
        first = LadderBackend(hook=lambda ctx: stop.set() if ctx.iteration_index == 0 else None)
        paused = await _solver(first, store, manager, ScriptedChannel(), clock).run(
            attempt, problem, stop=stop
        )
        assert paused.paused

        second = LadderBackend()
        outcome = await _solver(
            second, store, manager, ScriptedChannel(), clock, SolverPolicy(target_score=3.0)
        ).resume(attempt.attempt_id, problem)

        assert outcome.passed
        assert second.contexts[0].iteration_index == 1
        assert outcome.attempt.consumed.steps == 3
        assert outcome.iterations_completed == 3

    async def test_cancellation_checkpoints_before_propagating(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        class CancellingBackend(LadderBackend):
            async def propose(self, context):  # type: ignore[no-untyped-def]
                if context.iteration_index == 1:
                    raise asyncio.CancelledError
                return await super().propose(context)

        solver = _solver(CancellingBackend(), store, manager, ScriptedChannel(), clock)
        with pytest.raises(asyncio.CancelledError):
            await solver.run(attempt, problem)

        stored = await store.load_attempt(attempt.attempt_id)
        assert stored is not None
        assert stored.state is AttemptState.PAUSED
        assert stored.consumed.steps == 1


class TestTheBackendResumeToken:
    """The seam contracts reserve for the backend's own conversation state.

    Contracts call ``resume_token`` opaque and backend-defined; the solver's
    job is to carry it, durably, alongside the tokens that bought it. Without
    this a resumed attempt keeps its workspace and its budget but starts the
    model from nothing.
    """

    class _ResumableBackend(LadderBackend):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.restored: list[str] = []

        def checkpoint(self) -> str:
            return f"conv-{len(self.contexts)}"

        def restore(self, token: str) -> None:
            self.restored.append(token)

    async def test_the_token_is_persisted_with_the_spend_that_produced_it(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = self._ResumableBackend()
        outcome = await _solver(
            backend, store, manager, ScriptedChannel(), clock, SolverPolicy(target_score=2.0)
        ).run(attempt, problem)

        assert outcome.attempt.resume_token == "conv-2"
        stored = await store.load_attempt(attempt.attempt_id)
        assert stored is not None
        assert stored.resume_token == "conv-2"

    async def test_a_resumed_run_hands_the_token_back_to_the_backend(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        stop = asyncio.Event()
        first = self._ResumableBackend(
            hook=lambda ctx: stop.set() if ctx.iteration_index == 0 else None
        )
        paused = await _solver(first, store, manager, ScriptedChannel(), clock).run(
            attempt, problem, stop=stop
        )
        assert paused.paused
        assert paused.attempt.resume_token == "conv-1"

        second = self._ResumableBackend()
        await _solver(
            second, store, manager, ScriptedChannel(), clock, SolverPolicy(target_score=2.0)
        ).resume(attempt.attempt_id, problem)

        assert second.restored == ["conv-1"]

    async def test_a_backend_without_the_capability_is_fine(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        outcome = await _solver(
            LadderBackend(),
            store,
            manager,
            ScriptedChannel(),
            clock,
            SolverPolicy(target_score=1.0),
        ).run(attempt, problem)

        assert outcome.passed
        assert outcome.attempt.resume_token is None


class TestAcrossProcesses:
    async def test_a_file_backed_store_survives_being_closed_and_reopened(
        self,
        attempt: Attempt,
        problem: Problem,
        manager: WorkspaceManager,
        clock: FakeClock,
        tmp_path: Path,
    ) -> None:
        """The real thing: a second process picks the attempt up off disk.

        Two solvers, two stores, two backends, one database file. Only what
        was committed survives, which is exactly the guarantee being claimed.
        """
        db = tmp_path / "checkpoints.db"

        async with SqliteCheckpointStore(db) as first_store:
            crashing = CrashAfterStore(first_store, crash_after_commits=2)
            first_backend = ScriptedBackend(_proposals([4.0, 7.0]))
            with pytest.raises(SimulatedCrashError):
                await _solver(first_backend, crashing, manager, ScriptedChannel(), clock).run(
                    attempt, problem
                )

        async with SqliteCheckpointStore(db) as second_store:
            second_backend = ScriptedBackend(_proposals([4.0, 7.0])[1:])
            outcome = await _solver(
                second_backend,
                second_store,
                manager,
                ScriptedChannel(),
                clock,
                SolverPolicy(target_score=7.0),
            ).resume(attempt.attempt_id, problem)

            assert outcome.passed
            assert outcome.attempt.consumed.steps == 2
            assert outcome.attempt.consumed.tokens == 200
            assert second_backend.calls == 1
            assert second_backend.contexts[0].iteration_index == 1
            records = await second_store.iterations(attempt.attempt_id)
            assert [r.iteration_index for r in records] == [0, 1]
            assert all(r.phase is IterationPhase.VERIFIED for r in records)


class TestEscalatedPauseResumeClearsTheId:
    def test_pause_then_resume_from_escalated_cannot_abandon_without_a_fresh_id(
        self, attempt: Attempt
    ) -> None:
        """Solver crash-resume is not the only return to RUNNING; pause/resume is."""
        escalated = attempt.evolve(now_ms=1, state=AttemptState.ESCALATED, escalation_id="esc-a1-0")
        resumed = escalated.pause(now_ms=2).resume(now_ms=3)
        assert resumed.state is AttemptState.RUNNING
        assert resumed.escalation_id is None
        with pytest.raises(ContractViolationError, match="ABANDONED requires an escalation_id"):
            resumed.evolve(now_ms=4, state=AttemptState.ABANDONED)
