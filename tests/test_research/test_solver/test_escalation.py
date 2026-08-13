"""Escalation, not abandonment.

The invariant these tests defend: this writer may not quit. Every route by
which the solver might want to — the model declaring no viable approach,
refinement that stops paying, a verifier that will not run, a proposal that
tries to write outside its workspace — ends in a question to the operator
and a suspended attempt. ``Solver._abandon`` is a call-site guard on that
writer; ``Attempt.evolve`` can still reach ``ABANDONED`` with a leftover
``escalation_id``. The reply vocabulary is three words with no room for
advice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import (
    AttemptState,
    Cap,
    EscalationDecision,
    EscalationProtocolError,
    EscalationReason,
    EscalationVerdict,
    Problem,
    ProblemType,
    Split,
)
from turing.research.solver import FileEdit, Proposal, Solver, SolverPolicy
from turing.research.solver.models import IterationPhase

from .conftest import (
    SOLUTION_FILE,
    ExplodingVerifier,
    FakeClock,
    FlaggingVerifier,
    LadderBackend,
    MisattributingVerifier,
    ScriptedBackend,
    ScriptedChannel,
    fresh_with_cap,
    verdict_abandon,
    verdict_continue,
)

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import Attempt
    from turing.research.solver import InMemoryCheckpointStore, WorkspaceManager


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


class TestTheSolverCannotQuit:
    async def test_a_model_declaring_defeat_escalates_instead_of_stopping(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = ScriptedBackend(
            [Proposal(proposal_id="p0", rationale="I see no way forward", no_viable_approach=True)]
        )
        channel = ScriptedChannel()

        outcome = await _solver(backend, store, manager, channel, clock).run(attempt, problem)

        assert outcome.state is AttemptState.ESCALATED
        assert not outcome.terminal
        assert channel.requests[0].reason is EscalationReason.NO_VIABLE_APPROACH
        assert "no way forward" in channel.requests[0].summary

    async def test_defeat_followed_by_continue_goes_back_to_work(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = ScriptedBackend(
            [
                Proposal(proposal_id="p0", rationale="stuck", no_viable_approach=True),
                Proposal(
                    proposal_id="p1",
                    edits=(FileEdit(relative_path=SOLUTION_FILE, content="4.0"),),
                    tokens=10,
                ),
            ]
        )
        channel = ScriptedChannel([verdict_continue()])

        outcome = await _solver(
            backend, store, manager, channel, clock, SolverPolicy(target_score=4.0)
        ).run(attempt, problem)

        assert outcome.passed
        assert backend.calls == 2

    async def test_only_an_operator_abandon_reaches_abandoned(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = ScriptedBackend(
            [Proposal(proposal_id="p0", rationale="stuck", no_viable_approach=True)]
        )
        channel = ScriptedChannel([verdict_abandon()])

        outcome = await _solver(backend, store, manager, channel, clock).run(attempt, problem)

        assert outcome.state is AttemptState.ABANDONED
        assert outcome.terminal

    async def test_abandon_refuses_any_other_verdict(
        self,
        attempt: Attempt,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """The ``_abandon`` call-site guard rejects a non-ABANDON verdict.

        This is not a structural impossibility: ``Attempt.evolve`` can still
        reach ``ABANDONED`` with a leftover ``escalation_id``. The solver's
        own writer is what this tests.
        """
        solver = _solver(LadderBackend(), store, manager, ScriptedChannel(), clock)
        not_an_abandonment = EscalationDecision(
            request_id="esc-a1-0", verdict=EscalationVerdict.CONTINUE, decided_at_ms=1
        )

        with pytest.raises(EscalationProtocolError):
            await solver._abandon(attempt, not_an_abandonment)

    def test_the_solver_exposes_no_way_to_ask_it_to_stop(self) -> None:
        forbidden = {"abandon", "quit", "give_up", "cancel", "fail", "stop"}
        public = {name for name in dir(Solver) if not name.startswith("_")}
        assert public & forbidden == set()


class TestSuspendAndResume:
    async def test_no_answer_means_suspend_rather_than_block(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """The unattended case: ntfy fires at 2am and nobody is awake."""
        backend = ScriptedBackend(
            [Proposal(proposal_id="p0", rationale="stuck", no_viable_approach=True)]
        )
        channel = ScriptedChannel()

        outcome = await _solver(backend, store, manager, channel, clock).run(attempt, problem)

        assert outcome.suspended
        assert not outcome.attempt.is_resumable  # only a decision moves it
        stored = await store.load_attempt(attempt.attempt_id)
        assert stored is not None
        assert stored.state is AttemptState.ESCALATED
        assert stored.escalation_id == channel.requests[0].request_id

    async def test_a_later_run_re_polls_the_same_request(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = ScriptedBackend(
            [
                Proposal(proposal_id="p0", rationale="stuck", no_viable_approach=True),
                Proposal(
                    proposal_id="p1",
                    edits=(FileEdit(relative_path=SOLUTION_FILE, content="6.0"),),
                    tokens=10,
                ),
            ]
        )
        channel = ScriptedChannel()
        solver = _solver(backend, store, manager, channel, clock, SolverPolicy(target_score=6.0))

        suspended = await solver.run(attempt, problem)
        assert suspended.suspended
        request_id = channel.requests[0].request_id

        channel.queue(verdict_continue())
        resumed = await solver.resume(attempt.attempt_id, problem)

        assert resumed.passed
        assert len(channel.requests) == 1  # the same question, answered — not re-asked
        assert request_id.startswith("esc-a1-")

    async def test_a_decision_for_another_request_is_refused(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = ScriptedBackend(
            [Proposal(proposal_id="p0", rationale="stuck", no_viable_approach=True)]
        )

        def wrong_request(_request_id: str) -> EscalationDecision:
            return EscalationDecision(
                request_id="esc-someone-else-3",
                verdict=EscalationVerdict.CONTINUE,
                decided_at_ms=1,
            )

        channel = ScriptedChannel([wrong_request])

        with pytest.raises(EscalationProtocolError):
            await _solver(backend, store, manager, channel, clock).run(attempt, problem)

    async def test_the_escalation_is_persisted_for_whoever_resumes(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = ScriptedBackend(
            [Proposal(proposal_id="p0", rationale="stuck", no_viable_approach=True)]
        )
        channel = ScriptedChannel()

        outcome = await _solver(backend, store, manager, channel, clock).run(attempt, problem)

        stored = await store.load_escalation(channel.requests[0].request_id)
        assert stored == channel.requests[0]
        assert outcome.escalation == stored


class TestPatience:
    async def test_iterations_that_stop_paying_ask_a_human(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = ScriptedBackend(
            [
                Proposal(
                    proposal_id=f"p{i}",
                    edits=(FileEdit(relative_path=SOLUTION_FILE, content="2.0"),),
                    tokens=10,
                )
                for i in range(3)
            ]
        )
        channel = ScriptedChannel()

        outcome = await _solver(
            backend, store, manager, channel, clock, SolverPolicy(patience=2)
        ).run(attempt, problem)

        assert outcome.suspended
        assert channel.requests[0].reason is EscalationReason.NO_VIABLE_APPROACH
        assert backend.calls == 3  # first sets the best, two more stagnate

    async def test_a_run_of_regressions_asks_a_human(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = ScriptedBackend(
            [
                Proposal(
                    proposal_id=f"p{i}",
                    edits=(FileEdit(relative_path=SOLUTION_FILE, content=str(value)),),
                    tokens=10,
                )
                for i, value in enumerate([5.0, 4.0, 3.0])
            ]
        )
        channel = ScriptedChannel()

        outcome = await _solver(
            backend, store, manager, channel, clock, SolverPolicy(regression_patience=2)
        ).run(attempt, problem)

        assert outcome.suspended
        assert channel.requests[0].reason is EscalationReason.REPEATED_REGRESSION
        assert outcome.best_result is not None
        assert outcome.best_result.score == 5.0

    async def test_patience_never_terminates_an_attempt(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = ScriptedBackend(
            [
                Proposal(
                    proposal_id=f"p{i}",
                    edits=(FileEdit(relative_path=SOLUTION_FILE, content="2.0"),),
                    tokens=10,
                )
                for i in range(3)
            ]
        )
        outcome = await _solver(
            backend, store, manager, ScriptedChannel(), clock, SolverPolicy(patience=1)
        ).run(attempt, problem)

        assert outcome.state is AttemptState.ESCALATED
        assert not outcome.terminal


class TestHarnessFailures:
    async def test_a_verifier_that_will_not_run_asks_a_human(
        self,
        attempt: Attempt,
        template_dir: Path,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """An unrunnable verifier is a broken harness, not a failed solution.

        Scoring it zero would put a fabricated number into the trajectory.
        """
        problem = Problem(
            id="speedup-1",
            problem_type=ProblemType.SPEEDUP,
            goal="goal",
            workspace_template=template_dir,
            verifier=ExplodingVerifier(
                verifier_id="v-speedup-1",
                problem_id="speedup-1",
                description="broken",
                score_scale="speedup_ratio",
            ),
            split=Split.PRACTICE,
        )
        channel = ScriptedChannel()

        outcome = await _solver(LadderBackend(), store, manager, channel, clock).run(
            attempt, problem
        )

        assert outcome.suspended
        assert channel.requests[0].reason is EscalationReason.VERIFIER_UNRUNNABLE
        assert "timing harness is missing" in channel.requests[0].summary

    async def test_a_reported_harness_failure_asks_a_human_not_a_pass(
        self,
        attempt: Attempt,
        template_dir: Path,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """SpeedupVerifier never raises; it sets the measurement key instead.

        A result that claims correctness and a huge score is the shape that
        would mark the attempt PASSED if the solver treated it as a grade.
        """
        problem = Problem(
            id="speedup-1",
            problem_type=ProblemType.SPEEDUP,
            goal="goal",
            workspace_template=template_dir,
            verifier=FlaggingVerifier(
                verifier_id="v-speedup-1",
                problem_id="speedup-1",
                description="flags a dead harness",
                score_scale="speedup_ratio",
            ),
            split=Split.PRACTICE,
        )
        channel = ScriptedChannel()

        outcome = await _solver(
            LadderBackend(),
            store,
            manager,
            channel,
            clock,
            SolverPolicy(target_score=1.0),
        ).run(attempt, problem)

        assert outcome.suspended
        assert not outcome.passed
        assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
        assert "timing harness is missing" in channel.requests[0].summary
        assert outcome.best_result is None
        history = await store.iterations(attempt.attempt_id)
        assert history[-1].phase is IterationPhase.APPLIED
        assert history[-1].result is None

    async def test_a_later_harness_failure_does_not_replace_a_real_best(
        self,
        attempt: Attempt,
        template_dir: Path,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        problem = Problem(
            id="speedup-1",
            problem_type=ProblemType.SPEEDUP,
            goal="goal",
            workspace_template=template_dir,
            verifier=FlaggingVerifier(
                verifier_id="v-speedup-1",
                problem_id="speedup-1",
                description="succeeds once, then flags",
                score_scale="speedup_ratio",
                succeed_times=1,
            ),
            split=Split.PRACTICE,
        )
        channel = ScriptedChannel()
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=5, max_tokens=100_000, max_wall_clock_seconds=3600.0)
        )

        outcome = await _solver(LadderBackend(start=2.0), store, manager, channel, clock).run(
            attempt, problem
        )

        assert outcome.suspended
        assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
        assert outcome.best_result is not None
        assert outcome.best_result.score == pytest.approx(2.0)
        assert outcome.best_result.harness_failed is False

    async def test_a_result_for_the_wrong_problem_asks_a_human(
        self,
        attempt: Attempt,
        template_dir: Path,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        problem = Problem(
            id="speedup-1",
            problem_type=ProblemType.SPEEDUP,
            goal="goal",
            workspace_template=template_dir,
            verifier=MisattributingVerifier(
                verifier_id="v-speedup-1",
                problem_id="speedup-1",
                description="mislabels its output",
                score_scale="speedup_ratio",
            ),
            split=Split.PRACTICE,
        )
        channel = ScriptedChannel()

        outcome = await _solver(LadderBackend(), store, manager, channel, clock).run(
            attempt, problem
        )

        assert outcome.suspended
        assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
        assert outcome.best_result is None

    async def test_a_write_outside_the_workspace_asks_a_human(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = ScriptedBackend(
            [
                Proposal(
                    proposal_id="p0",
                    edits=(FileEdit(relative_path="../../pwned.txt", content="x"),),
                    tokens=10,
                )
            ]
        )
        channel = ScriptedChannel()

        outcome = await _solver(backend, store, manager, channel, clock).run(attempt, problem)

        assert outcome.suspended
        assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
        assert not (manager.root / "pwned.txt").exists()

    async def test_commands_without_a_runner_ask_a_human(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Executing model-authored commands is opt-in, owned by the safety layer."""
        backend = ScriptedBackend([Proposal(proposal_id="p0", commands=("rm -rf /",), tokens=10)])
        channel = ScriptedChannel()

        outcome = await _solver(backend, store, manager, channel, clock).run(attempt, problem)

        assert outcome.suspended
        assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
        assert "no CommandRunner is configured" in channel.requests[0].summary


class TestTheReplyVocabulary:
    def test_a_decision_carries_no_free_text(self) -> None:
        """Advice would make the operator the improvement mechanism.

        Contracts enforce this; the assertion lives here too because the
        solver is what would consume such a field, and a future edit adding
        "and pass it to the backend" has to fail somewhere the author is
        looking.
        """
        fields = set(EscalationDecision.__dataclass_fields__)
        assert fields == {"request_id", "verdict", "decided_at_ms", "cap_extension"}

    def test_the_verdict_vocabulary_is_exactly_three_words(self) -> None:
        assert {v.value for v in EscalationVerdict} == {"continue", "abandon", "extend_cap"}

    async def test_the_request_carries_context_outward(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Rich question out, three-valued answer back — the asymmetry is the point."""
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=2, max_tokens=10_000, max_wall_clock_seconds=3600.0)
        )
        channel = ScriptedChannel()

        await _solver(LadderBackend(), store, manager, channel, clock).run(attempt, problem)

        request = channel.requests[0]
        assert request.problem_id == problem.id
        assert request.attempt_id == attempt.attempt_id
        assert request.round_id == attempt.round_id
        assert request.consumed.steps == 2
        assert request.best_result is not None
        assert request.best_result.score == 2.0
