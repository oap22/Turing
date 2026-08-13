"""The refinement loop: propose, apply, evaluate, decide, repeat."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import AttemptState, Cap, CapConsumption, ContractViolationError
from turing.research.solver import (
    FileEdit,
    IterationPhase,
    Proposal,
    ProposalContext,
    Solver,
    SolverPolicy,
    better_result,
    summarise_progress,
)

from .conftest import (
    SOLUTION_FILE,
    FakeClock,
    LadderBackend,
    ScriptedBackend,
    ScriptedChannel,
    fresh_with_cap,
)

if TYPE_CHECKING:
    from turing.research.contracts import Attempt, Problem
    from turing.research.solver import InMemoryCheckpointStore, WorkspaceManager


def _edit(value: float) -> Proposal:
    return Proposal(
        proposal_id=f"p-{value}",
        rationale=f"try {value}",
        edits=(FileEdit(relative_path=SOLUTION_FILE, content=str(value)),),
        tokens=100,
    )


class TestHappyPath:
    async def test_refines_until_the_verifier_is_satisfied(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = LadderBackend(start=1.0, step=1.0)
        solver = Solver(
            backend=backend,
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(target_score=3.0),
            clock=clock,
        )

        outcome = await solver.run(attempt, problem)

        assert outcome.passed
        assert outcome.state is AttemptState.PASSED
        assert outcome.iterations_completed == 3
        assert outcome.best_result is not None
        assert outcome.best_result.score == 3.0
        assert backend.calls == 3

    async def test_the_workspace_holds_the_last_applied_change(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        solver = Solver(
            backend=LadderBackend(),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(target_score=2.0),
            clock=clock,
        )
        await solver.run(attempt, problem)

        assert (attempt.workspace_path / SOLUTION_FILE).read_text(encoding="utf-8") == "2.0"

    async def test_every_iteration_leaves_a_closed_journal_entry(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        solver = Solver(
            backend=LadderBackend(),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(target_score=2.0),
            clock=clock,
        )
        await solver.run(attempt, problem)

        records = await store.iterations(attempt.attempt_id)
        assert [r.iteration_index for r in records] == [0, 1]
        assert all(r.phase is IterationPhase.VERIFIED for r in records)
        assert all(r.result is not None for r in records)

    async def test_running_a_terminal_attempt_again_changes_nothing(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = LadderBackend()
        solver = Solver(
            backend=backend,
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(target_score=1.0),
            clock=clock,
        )
        first = await solver.run(attempt, problem)
        calls = backend.calls

        second = await solver.run(first.attempt, problem)

        assert second.state is AttemptState.PASSED
        assert backend.calls == calls

    async def test_propose_and_apply_writes_without_verifying(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """The runner-facing seam must not hold the verifier."""
        await manager.prepare(problem, attempt.workspace_path)
        backend = ScriptedBackend([_edit(2.0)])
        solver = Solver(
            backend=backend,
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            clock=clock,
        )
        proposal = await solver.propose_and_apply(
            ProposalContext(
                problem_id=problem.id,
                problem_type=problem.problem_type,
                goal=problem.goal,
                score_scale=problem.verifier.score_scale,
                workspace=attempt.workspace_path,
                iteration_index=0,
                seed=attempt.seed,
                remaining=CapConsumption(steps=5, tokens=1000, wall_clock_seconds=60.0),
            )
        )
        assert proposal.edits[0].content == "2.0"
        assert (attempt.workspace_path / SOLUTION_FILE).read_text(encoding="utf-8") == "2.0"
        assert await store.iterations(attempt.attempt_id) == ()
        assert attempt.consumed.steps == 0

    async def test_two_propose_and_apply_calls_on_one_live_backend(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """A live solver must not restore its own token over a spent ledger."""
        from turing.research.backends import FakeBackend, ProposalAdapter, encode_proposal

        await manager.prepare(problem, attempt.workspace_path)
        fake = FakeBackend.replying(
            encode_proposal(rationale="first", edits=((SOLUTION_FILE, "1.0"),)),
            encode_proposal(rationale="second", edits=((SOLUTION_FILE, "2.0"),)),
        )
        solver = Solver(
            backend=ProposalAdapter(fake),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            clock=clock,
        )
        remaining = CapConsumption(steps=5, tokens=1000, wall_clock_seconds=60.0)

        def ctx(index: int) -> ProposalContext:
            return ProposalContext(
                problem_id=problem.id,
                problem_type=problem.problem_type,
                goal=problem.goal,
                score_scale=problem.verifier.score_scale,
                workspace=attempt.workspace_path,
                iteration_index=index,
                seed=attempt.seed,
                remaining=remaining,
            )

        first = await solver.propose_and_apply(ctx(0))
        token = solver.backend_resume_token()
        assert token is not None
        assert first.tokens > 0
        second = await solver.propose_and_apply(ctx(1), resume_token=token)
        assert second.edits[0].content == "2.0"
        assert (attempt.workspace_path / SOLUTION_FILE).read_text(encoding="utf-8") == "2.0"
        assert fake.ledger.steps == 2

    async def test_generate_failure_carries_spend_through_propose_and_apply(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """A failed generate() must surface accounted usage, not drop it on the ledger."""
        from turing.research.backends import FakeBackend, ProposalAdapter
        from turing.research.backends.errors import BackendError
        from turing.research.backends.fake import ScriptedTurn

        await manager.prepare(problem, attempt.workspace_path)
        fake = FakeBackend([ScriptedTurn(error=BackendError("provider down"))])
        solver = Solver(
            backend=ProposalAdapter(fake),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            clock=clock,
        )
        with pytest.raises(BackendError, match="provider down") as caught:
            await solver.propose_and_apply(
                ProposalContext(
                    problem_id=problem.id,
                    problem_type=problem.problem_type,
                    goal=problem.goal,
                    score_scale=problem.verifier.score_scale,
                    workspace=attempt.workspace_path,
                    iteration_index=0,
                    seed=attempt.seed,
                    remaining=CapConsumption(steps=5, tokens=1000, wall_clock_seconds=60.0),
                )
            )
        assert caught.value.usage is not None
        assert caught.value.usage.estimated is True
        assert caught.value.usage.total_tokens == fake.ledger.tokens
        assert fake.ledger.tokens > 0
        assert fake.ledger.steps == 1

    async def test_explicit_zero_usage_falls_back_through_propose_and_apply(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """BackendError(usage=Usage()) must carry the ledger estimate, not zero."""
        from turing.research.backends import FakeBackend, ProposalAdapter, Usage
        from turing.research.backends.errors import BackendError
        from turing.research.backends.fake import ScriptedTurn

        await manager.prepare(problem, attempt.workspace_path)
        fake = FakeBackend([ScriptedTurn(error=BackendError("provider down", usage=Usage()))])
        solver = Solver(
            backend=ProposalAdapter(fake),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            clock=clock,
        )
        with pytest.raises(BackendError, match="provider down") as caught:
            await solver.propose_and_apply(
                ProposalContext(
                    problem_id=problem.id,
                    problem_type=problem.problem_type,
                    goal=problem.goal,
                    score_scale=problem.verifier.score_scale,
                    workspace=attempt.workspace_path,
                    iteration_index=0,
                    seed=attempt.seed,
                    remaining=CapConsumption(steps=5, tokens=1000, wall_clock_seconds=60.0),
                )
            )
        assert caught.value.usage is not None
        assert caught.value.usage.estimated is True
        assert caught.value.usage.total_tokens == fake.ledger.tokens
        assert fake.ledger.tokens > 0
        assert fake.ledger.steps == 1

    async def test_foreign_spend_on_a_non_backend_error_is_replaced(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """propose_and_apply must surface the ledger charge, not a foreign 1."""
        from turing.research.backends import FakeBackend, ProposalAdapter, Usage
        from turing.research.backends.fake import ScriptedTurn

        class SdkError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("sdk blew up")
                self.usage = Usage(input_tokens=1)
                self.tokens = 1

        await manager.prepare(problem, attempt.workspace_path)
        fake = FakeBackend([ScriptedTurn(error=SdkError())])
        solver = Solver(
            backend=ProposalAdapter(fake),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            clock=clock,
        )
        with pytest.raises(SdkError) as caught:
            await solver.propose_and_apply(
                ProposalContext(
                    problem_id=problem.id,
                    problem_type=problem.problem_type,
                    goal=problem.goal,
                    score_scale=problem.verifier.score_scale,
                    workspace=attempt.workspace_path,
                    iteration_index=0,
                    seed=attempt.seed,
                    remaining=CapConsumption(steps=5, tokens=1000, wall_clock_seconds=60.0),
                )
            )
        charged = fake.ledger.tokens
        assert charged > 1
        assert caught.value.usage.total_tokens == charged
        assert caught.value.tokens == charged
        assert fake.ledger.steps == 1

    async def test_read_only_usage_on_a_non_backend_error_is_enveloped(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """propose_and_apply must surface the ledger charge, not a frozen usage of 1."""
        from turing.research.backends import FakeBackend, ProposalAdapter, Usage
        from turing.research.backends.errors import BackendError
        from turing.research.backends.fake import ScriptedTurn

        class SdkError(RuntimeError):
            @property
            def usage(self) -> Usage:
                return Usage(input_tokens=0, output_tokens=1)

        await manager.prepare(problem, attempt.workspace_path)
        fake = FakeBackend([ScriptedTurn(error=SdkError("sdk blew up"))])
        solver = Solver(
            backend=ProposalAdapter(fake),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            clock=clock,
        )
        with pytest.raises(BackendError) as caught:
            await solver.propose_and_apply(
                ProposalContext(
                    problem_id=problem.id,
                    problem_type=problem.problem_type,
                    goal=problem.goal,
                    score_scale=problem.verifier.score_scale,
                    workspace=attempt.workspace_path,
                    iteration_index=0,
                    seed=attempt.seed,
                    remaining=CapConsumption(steps=5, tokens=1000, wall_clock_seconds=60.0),
                )
            )
        charged = fake.ledger.tokens
        assert charged > 1
        assert caught.value.usage is not None
        assert caught.value.usage.total_tokens == charged
        assert isinstance(caught.value.__cause__, SdkError)
        assert fake.ledger.steps == 1


class TestScoring:
    async def test_the_best_result_is_kept_when_a_later_iteration_regresses(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Continuous scoring: the run reports its best, not its last."""
        backend = ScriptedBackend([_edit(5.0), _edit(2.0), _edit(3.0)])
        solver = Solver(
            backend=backend,
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(),
            clock=clock,
            # cap keeps the run to exactly the scripted length
        )
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=3, max_tokens=100_000, max_wall_clock_seconds=3600.0)
        )

        outcome = await solver.run(attempt, problem)

        assert outcome.best_result is not None
        assert outcome.best_result.score == 5.0

    async def test_correctness_beats_score(self) -> None:
        """A fast-but-wrong solution never outranks a slow-but-right one."""
        from turing.research.contracts import VerificationResult

        wrong = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=99.0,
            passed_correctness=False,
            score_scale="speedup_ratio",
        )
        right = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=1.2,
            passed_correctness=True,
            score_scale="speedup_ratio",
        )
        assert better_result(wrong, right) is right
        assert better_result(right, wrong) is right

    def test_a_harness_failure_never_wins_best(self) -> None:
        from turing.research.contracts import HARNESS_FAILURE_KEY, VerificationResult

        grade = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=1.2,
            passed_correctness=True,
            score_scale="speedup_ratio",
        )
        broken = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=99.0,
            passed_correctness=True,
            score_scale="speedup_ratio",
            raw_measurements={HARNESS_FAILURE_KEY: 1.0},
        )
        assert better_result(None, broken) is None
        assert better_result(grade, broken) is grade
        assert better_result(broken, grade) is grade
        assert better_result(broken, None) is None
        assert better_result(broken, broken) is None

    async def test_an_incorrect_solution_never_satisfies_the_bar(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """The verifier scores an unparseable file 0.0 and marks it incorrect."""
        backend = ScriptedBackend(
            [
                Proposal(
                    proposal_id="p0",
                    edits=(FileEdit(relative_path=SOLUTION_FILE, content="not a number"),),
                    tokens=10,
                )
            ]
        )
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=1, max_tokens=100_000, max_wall_clock_seconds=3600.0)
        )
        solver = Solver(
            backend=backend,
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(target_score=0.0),
            clock=clock,
        )

        outcome = await solver.run(attempt, problem)

        assert not outcome.passed
        assert outcome.best_result is not None
        assert outcome.best_result.passed_correctness is False

    def test_a_policy_with_no_target_can_never_pass(self) -> None:
        """A continuous score has no "done"; refine until the cap instead."""
        from turing.research.contracts import VerificationResult

        result = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=1e9,
            passed_correctness=True,
            score_scale="speedup_ratio",
        )
        assert SolverPolicy().satisfied_by(result) is False
        assert SolverPolicy(target_score=1e9).satisfied_by(result) is True

    def test_a_harness_failure_cannot_satisfy_the_bar(self) -> None:
        from turing.research.contracts import HARNESS_FAILURE_KEY, VerificationResult

        broken = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=1e9,
            passed_correctness=True,
            score_scale="speedup_ratio",
            raw_measurements={HARNESS_FAILURE_KEY: 1.0},
        )
        assert SolverPolicy(target_score=1.0).satisfied_by(broken) is False
        assert (
            SolverPolicy(target_score=1.0, require_correctness=False).satisfied_by(broken) is False
        )


class TestContext:
    async def test_the_backend_sees_history_and_shrinking_budget(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = LadderBackend(tokens=100)
        solver = Solver(
            backend=backend,
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(target_score=3.0),
            clock=clock,
        )
        await solver.run(attempt, problem)

        first, second, third = backend.contexts
        assert first.iteration_index == 0
        assert first.history == ()
        assert first.last_result is None
        assert first.goal == problem.goal
        assert first.seed == attempt.seed

        assert len(second.history) == 1
        assert second.last_result is not None
        assert second.last_result.score == 1.0
        assert second.remaining.tokens == first.remaining.tokens - 100
        assert second.remaining.steps == first.remaining.steps - 1

        assert third.best_result is not None
        assert third.best_result.score == 2.0

    async def test_the_context_never_carries_the_verifier(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """The bar the agent is graded on is not reachable from the model call.

        Contracts make a verifier immutable three ways over. Withholding the
        reference means none of those defences ever has to fire.
        """
        backend = LadderBackend()
        solver = Solver(
            backend=backend,
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(target_score=1.0),
            clock=clock,
        )
        await solver.run(attempt, problem)

        (context,) = backend.contexts
        values = [getattr(context, f) for f in context.__slots__]
        assert problem.verifier not in values
        assert problem not in values


class TestGuards:
    async def test_an_attempt_for_another_problem_is_refused(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        solver = Solver(
            backend=LadderBackend(),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            clock=clock,
        )
        mismatched = attempt.evolve(now_ms=1, problem_id="kaggle-2")

        with pytest.raises(ContractViolationError):
            await solver.run(mismatched, problem)

    async def test_running_behind_a_newer_checkpoint_is_refused(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Two processes on one attempt is a bug; a silent rewind is worse."""
        from turing.research.solver import CheckpointError

        await store.save_attempt(attempt.evolve(now_ms=1, checkpoint_seq=9))
        solver = Solver(
            backend=LadderBackend(),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            clock=clock,
        )

        with pytest.raises(CheckpointError):
            await solver.run(attempt, problem)

    async def test_resume_needs_a_stored_checkpoint(
        self,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        from turing.research.solver import CheckpointError

        solver = Solver(
            backend=LadderBackend(),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            clock=clock,
        )
        with pytest.raises(CheckpointError):
            await solver.resume("never-seen", problem)


class TestProgressSummary:
    def test_stagnation_and_regression_are_counted_separately(self) -> None:
        from turing.research.contracts import VerificationResult

        def r(score: float) -> VerificationResult:
            return VerificationResult(
                problem_id="p",
                verifier_id="v",
                score=score,
                passed_correctness=True,
                score_scale="speedup_ratio",
            )

        progress = summarise_progress([r(1.0), r(3.0), r(2.0), r(1.5)])

        assert progress.best is not None
        assert progress.best.score == 3.0
        assert progress.stagnant_iterations == 2
        assert progress.consecutive_regressions == 2
        assert progress.verified_iterations == 4

    def test_an_empty_history_summarises_to_nothing(self) -> None:
        progress = summarise_progress([])
        assert progress.best is None
        assert progress.stagnant_iterations == 0
        assert progress.verified_iterations == 0

    def test_a_harness_failure_is_not_a_verification(self) -> None:
        from turing.research.contracts import HARNESS_FAILURE_KEY, VerificationResult

        def r(score: float) -> VerificationResult:
            return VerificationResult(
                problem_id="p",
                verifier_id="v",
                score=score,
                passed_correctness=True,
                score_scale="speedup_ratio",
            )

        broken = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=99.0,
            passed_correctness=True,
            score_scale="speedup_ratio",
            raw_measurements={HARNESS_FAILURE_KEY: 1.0},
        )
        progress = summarise_progress([broken, r(1.2), broken])
        assert progress.best is not None
        assert progress.best.score == pytest.approx(1.2)
        assert progress.verified_iterations == 1
        assert progress.stagnant_iterations == 0


@pytest.mark.parametrize("bad", ["", "   "])
def test_a_proposal_needs_edits_with_real_paths(bad: str) -> None:
    from turing.research.solver import ProposalError

    with pytest.raises(ProposalError):
        FileEdit(relative_path=bad, content="x")


def test_a_proposal_cannot_both_give_up_and_change_things() -> None:
    from turing.research.solver import ProposalError

    with pytest.raises(ProposalError):
        Proposal(
            proposal_id="p0",
            no_viable_approach=True,
            edits=(FileEdit(relative_path=SOLUTION_FILE, content="1.0"),),
        )


def test_a_proposal_rejects_two_writes_to_one_path() -> None:
    from turing.research.solver import ProposalError

    with pytest.raises(ProposalError):
        Proposal(
            proposal_id="p0",
            edits=(
                FileEdit(relative_path=SOLUTION_FILE, content="1.0"),
                FileEdit(relative_path=SOLUTION_FILE, content="2.0"),
            ),
        )
