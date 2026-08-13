"""``RoundRunner`` behaviour, against a fake solver.

These are the instrument's own guarantees. Every one of them has to hold for an
arbitrary solver, so the solver here is scripted, broken, or adversarial by
turns and never real.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import (
    Attempt,
    AttemptState,
    Cap,
    CapExtension,
    ContractViolationError,
    EngineIdentity,
    EscalationDecision,
    EscalationProtocolError,
    EscalationReason,
    EscalationVerdict,
    Problem,
    ProblemType,
    Split,
)
from turing.research.loop.metrics import (
    NoiseFloorStatistic,
    SaturationVerdict,
    build_type_scores,
    measure_noise_floor,
)
from turing.research.loop.noise_floor import NoiseFloorReport
from turing.research.loop.protocols import SolverStep, SolverTask
from turing.research.loop.runner import PassCriterion, RoundRunner

from .conftest import (
    DEFAULT_CAP,
    ENGINE,
    ExplodingSolver,
    FakeSolver,
    ScriptedEscalationChannel,
    ScriptedVerifier,
    make_config,
    make_problem,
    make_runner,
)

if TYPE_CHECKING:
    from turing.research.loop.trajectory import TrajectoryStore

    from .conftest import FakeClock, TempWorkspaceProvider


def floors_for(
    problems: list[Problem],
    values: dict[int, float],
    *,
    eval_set_hash: str | None = None,
    engine: EngineIdentity = ENGINE,
) -> NoiseFloorReport:
    """Build a provenanced noise-floor report from synthetic per-seed cells."""
    from turing.research.problems.adapter import fingerprint_corpus

    from .test_metrics import scored

    per_seed = {
        seed: build_type_scores(
            [scored(p.id, value, problem_type=p.problem_type, split=p.split) for p in problems]
        )
        for seed, value in values.items()
    }
    return NoiseFloorReport(
        run_id="nf-test",
        eval_set_hash=eval_set_hash if eval_set_hash is not None else fingerprint_corpus(problems),
        engine=engine,
        seeds=tuple(sorted(values)),
        floors=measure_noise_floor(per_seed),
        per_seed_scores=per_seed,
        statistic=NoiseFloorStatistic.STDEV,
        escalation_count=0,
        created_at_ms=0,
    )


# --------------------------------------------------------------------------- #
# The cap is the runner's, not the solver's
# --------------------------------------------------------------------------- #


class TestCapEnforcement:
    async def test_runner_charges_a_step_even_when_the_solver_reports_none(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """A solver that reports zero consumption must still hit the cap."""
        solver = FakeSolver([SolverStep(tokens=0, wall_clock_seconds=0.0)])
        runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        problem = make_problem("s1")
        outcome = await runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0))
        assert outcome.attempt.consumed.steps == DEFAULT_CAP.max_steps
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP

    async def test_wall_clock_cap_ignores_a_solver_that_reports_zero(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The runner's clock, not the solver's self-report, is what the cap reads."""

        class SlowSolver:
            async def step(self, task: SolverTask, attempt: Attempt) -> SolverStep:
                clock.advance(10.0)
                return SolverStep(tokens=1, wall_clock_seconds=0.0)

        cap = Cap(max_steps=1000, max_tokens=100_000, max_wall_clock_seconds=1.0)
        runner = make_runner(solver=SlowSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_attempt(
            make_problem("s1", default_cap=cap),
            make_config(cap=cap),
            output_dir=store.round_dir(0),
        )
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert outcome.attempt.consumed.wall_clock_seconds >= 1.0
        assert outcome.attempt.consumed.steps < 1000

    async def test_verifier_time_counts_against_the_wall_clock_cap(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Harness time is the expensive part; it is not free against the cap."""

        @dataclass(frozen=True)
        class SlowVerifier(ScriptedVerifier):
            async def verify(self, workspace: Path):  # type: ignore[no-untyped-def]
                clock.advance(10.0)
                return await super().verify(workspace)

        cap = Cap(max_steps=1000, max_tokens=100_000, max_wall_clock_seconds=1.0)
        problem = Problem(
            id="s1",
            problem_type=ProblemType.SPEEDUP,
            goal="make s1 better",
            workspace_template=Path("/nonexistent/template"),
            verifier=SlowVerifier(
                verifier_id="v-s1",
                problem_id="s1",
                description="slow harness",
                score_scale="speedup_ratio",
            ),
            split=Split.PRACTICE,
            default_cap=cap,
        )
        runner = make_runner(
            solver=FakeSolver([SolverStep(tokens=1, wall_clock_seconds=0.0)]),
            store=store,
            workspaces=workspaces,
            clock=clock,
        )
        outcome = await runner.run_attempt(
            problem, make_config(cap=cap), output_dir=store.round_dir(0)
        )
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert outcome.attempt.consumed.wall_clock_seconds >= 1.0
        assert outcome.attempt.consumed.steps < 1000

    async def test_an_over_budget_pass_is_failed_within_cap(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Meeting the bar after the cap is gone is not a pass."""

        @dataclass(frozen=True)
        class SlowVerifier(ScriptedVerifier):
            async def verify(self, workspace: Path):  # type: ignore[no-untyped-def]
                clock.advance(10.0)
                return await super().verify(workspace)

        cap = Cap(max_steps=1000, max_tokens=100_000, max_wall_clock_seconds=1.0)
        problem = Problem(
            id="s1",
            problem_type=ProblemType.SPEEDUP,
            goal="make s1 better",
            workspace_template=Path("/nonexistent/template"),
            verifier=SlowVerifier(
                verifier_id="v-s1",
                problem_id="s1",
                description="slow harness",
                score_scale="speedup_ratio",
                scores=(5.0,),
            ),
            split=Split.PRACTICE,
            default_cap=cap,
        )
        runner = make_runner(
            solver=FakeSolver([SolverStep(tokens=1, wall_clock_seconds=0.0)]),
            store=store,
            workspaces=workspaces,
            clock=clock,
        )
        outcome = await runner.run_attempt(
            problem,
            make_config(cap=cap, pass_criteria={"s1": PassCriterion(min_score=2.0)}),
            output_dir=store.round_dir(0),
        )
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert outcome.best_result is not None
        assert outcome.best_result.score == pytest.approx(5.0)
        assert outcome.attempt.consumed.wall_clock_seconds >= 1.0
        assert len(problem.verifier.calls) == 1

    async def test_an_exhausted_step_does_not_run_the_verifier(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """A step that already blew the cap does not get a free verify."""

        class SlowSolver:
            async def step(self, task: SolverTask, attempt: Attempt) -> SolverStep:
                clock.advance(10.0)
                return SolverStep(tokens=1, wall_clock_seconds=0.0)

        cap = Cap(max_steps=1000, max_tokens=100_000, max_wall_clock_seconds=1.0)
        problem = make_problem("s1", scores=(5.0,), default_cap=cap)
        runner = make_runner(solver=SlowSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_attempt(
            problem,
            make_config(cap=cap, pass_criteria={"s1": PassCriterion(min_score=2.0)}),
            output_dir=store.round_dir(0),
        )
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert problem.verifier.calls == []

    async def test_an_over_budget_escalation_request_does_not_fire(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        class SlowSolver:
            async def step(self, task: SolverTask, attempt: Attempt) -> SolverStep:
                clock.advance(10.0)
                return SolverStep(
                    tokens=1,
                    wall_clock_seconds=0.0,
                    escalate=EscalationReason.NO_VIABLE_APPROACH,
                )

        cap = Cap(max_steps=1000, max_tokens=100_000, max_wall_clock_seconds=1.0)
        channel = ScriptedEscalationChannel([EscalationVerdict.ABANDON])
        runner = make_runner(
            solver=SlowSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1", default_cap=cap),
            make_config(cap=cap),
            output_dir=store.round_dir(0),
        )
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert channel.requests == []

    def test_a_solver_cannot_report_its_own_step_count(self) -> None:
        assert not hasattr(SolverStep(), "steps")
        assert SolverStep(tokens=5).consumption.steps == 1

    async def test_cap_exhaustion_does_not_escalate_by_default(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Otherwise human-gate load is pinned at one per problem forever."""
        channel = ScriptedEscalationChannel()
        runner = make_runner(
            solver=FakeSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1"), make_config(), output_dir=store.round_dir(0)
        )
        assert channel.requests == []
        assert outcome.escalation_count == 0
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP

    async def test_cap_exhaustion_escalates_when_the_policy_says_so(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        channel = ScriptedEscalationChannel(
            [
                EscalationDecision(
                    request_id="ignored",
                    verdict=EscalationVerdict.EXTEND_CAP,
                    decided_at_ms=1,
                    cap_extension=CapExtension(extra_steps=2),
                ),
                EscalationVerdict.CONTINUE,
            ]
        )
        runner = make_runner(
            solver=FakeSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1"),
            make_config(escalate_on_cap_exhaustion=True),
            output_dir=store.round_dir(0),
        )
        assert channel.requests[0].reason is EscalationReason.CAP_EXHAUSTED
        # The extension bought two more steps, then CONTINUE with no budget ends it.
        assert outcome.attempt.cap.max_steps == DEFAULT_CAP.max_steps + 2
        assert outcome.attempt.cap.extension_count == 1
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP


def _has_callable_verify(value: object) -> bool:
    return callable(getattr(value, "verify", None))


def _task_exposes_callable_verify(task: SolverTask) -> bool:
    for field in fields(task):
        value = getattr(task, field.name)
        if _has_callable_verify(value):
            return True
        if isinstance(value, tuple) and any(_has_callable_verify(item) for item in value):
            return True
    return False


# --------------------------------------------------------------------------- #
# The verifier is the runner's, not the solver's
# --------------------------------------------------------------------------- #


class TestTheVerifierIsNotHandedToTheSolver:
    def test_the_task_cannot_carry_a_verifier(self) -> None:
        names = {f.name for f in fields(SolverTask)}
        assert "verifier" not in names
        assert "workspace_template" not in names

    def test_tags_that_include_the_verifier_are_refused(self) -> None:
        """A Problem whose tags include the verifier must not yield callable verify."""
        problem = make_problem("s1")
        smuggled = Problem(
            id=problem.id,
            problem_type=problem.problem_type,
            goal=problem.goal,
            workspace_template=problem.workspace_template,
            verifier=problem.verifier,
            split=problem.split,
            tags=(problem.verifier,),  # type: ignore[arg-type]
        )
        with pytest.raises(ContractViolationError, match="plain str"):
            SolverTask.from_problem(smuggled)

    def test_plain_string_tags_survive(self) -> None:
        problem = make_problem("s1")
        tagged = Problem(
            id=problem.id,
            problem_type=problem.problem_type,
            goal=problem.goal,
            workspace_template=problem.workspace_template,
            verifier=problem.verifier,
            split=problem.split,
            tags=("turing", "onnx"),
        )
        task = SolverTask.from_problem(tagged)
        assert task.tags == ("turing", "onnx")
        assert not _task_exposes_callable_verify(task)

    def test_a_verifier_as_score_scale_is_refused(self) -> None:
        host = make_problem("s1")
        with pytest.raises(ContractViolationError, match="score_scale"):
            SolverTask(
                id=host.id,
                problem_type=host.problem_type,
                goal=host.goal,
                split=host.split,
                score_scale=host.verifier,  # type: ignore[arg-type]
            )

    async def test_a_capturing_solver_cannot_verify_off_budget(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Finding 11: capturing problem.verifier and calling verify is off-budget."""

        class CapturingSolver:
            def __init__(self) -> None:
                self.seen: SolverTask | None = None
                self.off_budget = 0

            async def step(self, task: SolverTask, attempt: Attempt) -> SolverStep:
                self.seen = task
                verifier = getattr(task, "verifier", None)
                if verifier is None:
                    for field in fields(task):
                        value = getattr(task, field.name)
                        if _has_callable_verify(value):
                            verifier = value
                            break
                        if isinstance(value, tuple):
                            for item in value:
                                if _has_callable_verify(item):
                                    verifier = item
                                    break
                if verifier is not None:
                    await verifier.verify(attempt.workspace_path)
                    self.off_budget += 1
                return SolverStep(tokens=1)

        solver = CapturingSolver()
        problem = make_problem("s1", scores=(0.5,))
        runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0))
        assert isinstance(solver.seen, SolverTask)
        assert not hasattr(solver.seen, "verifier")
        assert not _task_exposes_callable_verify(solver.seen)
        assert solver.off_budget == 0
        assert problem.verifier.calls, "the runner still verifies; the solver does not"


# --------------------------------------------------------------------------- #
# Verification is the runner's job
# --------------------------------------------------------------------------- #


class TestVerification:
    async def test_best_result_wins_and_correctness_dominates(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        cap = Cap(max_steps=10, max_tokens=10_000, max_wall_clock_seconds=600.0)
        problem = make_problem(
            "s1",
            scores=(9.0, 2.0, 3.0),
            correctness=(False, True, True),
            default_cap=cap,
        )
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_attempt(
            problem, make_config(cap=cap), output_dir=store.round_dir(0)
        )
        assert outcome.best_result is not None
        # 9.0 was faster but wrong; later correct scores replace it. The
        # reported number is the latest correct workspace, not max(2.0, 3.0).
        assert outcome.best_result.score == pytest.approx(3.0)
        assert outcome.best_result.passed_correctness is True

    async def test_reported_score_is_the_latest_verify_not_the_max(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Max-of-N on a timing harness grows with the cap at zero capability change."""
        scores = (1.2, 2.0, 0.9, 1.5, 2.0)
        cap = Cap(max_steps=4, max_tokens=10_000, max_wall_clock_seconds=600.0)
        problem = make_problem("s1", scores=scores, default_cap=cap)
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_attempt(
            problem, make_config(cap=cap), output_dir=store.round_dir(0)
        )
        assert outcome.best_result is not None
        # Steps 1–3 verify (step 4 trips the cap and is not verified).
        # Last in-cap score is 0.9; max of the prefix is 2.0.
        assert outcome.best_result.score == pytest.approx(0.9)
        assert outcome.best_result.score != pytest.approx(2.0)

    async def test_pass_criterion_ends_the_attempt_early(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        problem = make_problem("s1", scores=(1.0, 5.0))
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        config = make_config(pass_criteria={"s1": PassCriterion(min_score=2.0)})
        outcome = await runner.run_attempt(problem, config, output_dir=store.round_dir(0))
        assert outcome.attempt.state is AttemptState.PASSED
        assert outcome.attempt.consumed.steps == 2

    async def test_no_criterion_means_run_to_the_cap(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """A speedup baseline is already correct; correctness is not the bar."""
        problem = make_problem("s1", scores=(1.0,), correctness=(True,))
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0))
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert outcome.best_result is not None

    async def test_verify_every_step_false_still_scores_the_attempt(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The flag skips *per-step* verifies, not verification itself."""
        problem = make_problem("s1", scores=(4.0,))
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_attempt(
            problem,
            make_config(verify_every_step=False),
            output_dir=store.round_dir(0),
        )
        assert outcome.best_result is not None
        assert outcome.best_result.score == pytest.approx(4.0)
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert len(problem.verifier.calls) == 1
        assert outcome.attempt.consumed.steps == DEFAULT_CAP.max_steps

    async def test_verify_every_step_false_still_escalates_a_dead_harness(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        channel = ScriptedEscalationChannel([EscalationVerdict.ABANDON])
        runner = make_runner(
            solver=FakeSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1", harness_failure=True),
            make_config(verify_every_step=False),
            output_dir=store.round_dir(0),
        )
        assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
        assert outcome.attempt.state is AttemptState.ABANDONED
        assert outcome.best_result is None

    async def test_a_broken_verifier_escalates_rather_than_scoring_zero(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        channel = ScriptedEscalationChannel([EscalationVerdict.ABANDON])
        runner = make_runner(
            solver=FakeSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1", raises=True), make_config(), output_dir=store.round_dir(0)
        )
        assert channel.requests[0].reason is EscalationReason.VERIFIER_UNRUNNABLE
        assert outcome.attempt.state is AttemptState.ABANDONED

    async def test_a_reported_harness_failure_escalates_rather_than_scoring_zero(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The verifier does not raise for a dead harness; it sets the measurement key."""
        channel = ScriptedEscalationChannel([EscalationVerdict.ABANDON])
        runner = make_runner(
            solver=FakeSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1", harness_failure=True),
            make_config(),
            output_dir=store.round_dir(0),
        )
        assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
        assert outcome.attempt.state is AttemptState.ABANDONED
        assert outcome.best_result is None

    async def test_a_broken_solver_is_a_harness_failure(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        channel = ScriptedEscalationChannel([EscalationVerdict.ABANDON])
        runner = make_runner(
            solver=ExplodingSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1"), make_config(), output_dir=store.round_dir(0)
        )
        assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
        assert outcome.attempt.state is AttemptState.ABANDONED


# --------------------------------------------------------------------------- #
# Escalation: the agent asks, it never quits
# --------------------------------------------------------------------------- #


class TestEscalation:
    async def test_solver_request_suspends_the_loop_and_resumes_on_continue(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        solver = FakeSolver(
            [
                SolverStep(tokens=1, note="stuck", escalate=EscalationReason.NO_VIABLE_APPROACH),
                SolverStep(tokens=1, note="carrying on"),
            ]
        )
        channel = ScriptedEscalationChannel([EscalationVerdict.CONTINUE])
        runner = make_runner(
            solver=solver,
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1"), make_config(), output_dir=store.round_dir(0)
        )
        assert len(channel.requests) == 1
        assert channel.requests[0].reason is EscalationReason.NO_VIABLE_APPROACH
        # It resumed: more steps ran after the decision, and it ended on the cap.
        assert outcome.attempt.consumed.steps == DEFAULT_CAP.max_steps
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert outcome.decisions[0].verdict is EscalationVerdict.CONTINUE

    async def test_only_an_operator_abandon_reaches_abandoned(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        solver = FakeSolver([SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH)])
        channel = ScriptedEscalationChannel([EscalationVerdict.ABANDON])
        runner = make_runner(
            solver=solver,
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1"), make_config(), output_dir=store.round_dir(0)
        )
        assert outcome.attempt.state is AttemptState.ABANDONED
        assert outcome.attempt.escalation_id == channel.requests[0].request_id

    async def test_a_solver_has_no_way_to_reach_abandoned_on_its_own(self) -> None:
        """There is no ``give_up`` field, and every solver-driven exit escalates."""
        assert not hasattr(SolverStep(), "give_up")
        assert not hasattr(SolverStep(), "abandon")
        assert SolverStep().escalate is None

    async def test_extend_cap_grows_the_budget_and_the_attempt_keeps_going(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        solver = FakeSolver(
            [
                SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH),
                SolverStep(tokens=1),
            ]
        )
        channel = ScriptedEscalationChannel(
            [
                EscalationDecision(
                    request_id="ignored",
                    verdict=EscalationVerdict.EXTEND_CAP,
                    decided_at_ms=1,
                    cap_extension=CapExtension(extra_steps=5),
                )
            ]
        )
        runner = make_runner(
            solver=solver,
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1"), make_config(), output_dir=store.round_dir(0)
        )
        assert outcome.attempt.cap.max_steps == DEFAULT_CAP.max_steps + 5
        assert outcome.attempt.cap.extension_count == 1
        assert outcome.attempt.consumed.steps == DEFAULT_CAP.max_steps + 5

    @pytest.mark.parametrize(
        "verdict",
        [
            EscalationVerdict.CONTINUE,
            EscalationDecision(
                request_id="ignored",
                verdict=EscalationVerdict.EXTEND_CAP,
                decided_at_ms=1,
                cap_extension=CapExtension(extra_steps=5),
            ),
        ],
        ids=["continue", "extend_cap"],
    )
    async def test_continue_and_extend_cap_clear_the_escalation_id(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        verdict: EscalationVerdict | EscalationDecision,
    ) -> None:
        """A leftover id would let evolve(state=ABANDONED) skip the operator."""
        solver = FakeSolver(
            [
                SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH),
                SolverStep(tokens=1),
            ]
        )
        channel = ScriptedEscalationChannel([verdict])
        runner = make_runner(
            solver=solver,
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1"), make_config(), output_dir=store.round_dir(0)
        )
        assert outcome.attempt.escalation_id is None
        with pytest.raises(ContractViolationError, match="ABANDONED requires an escalation_id"):
            outcome.attempt.evolve(now_ms=clock.now_ms(), state=AttemptState.ABANDONED)

    async def test_the_attempt_is_checkpointed_before_it_suspends(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """A crash while waiting on a human must cost the remainder, not the attempt."""
        seen: list[str] = []

        class CheckpointObservingChannel(ScriptedEscalationChannel):
            async def request_decision(self, request):  # type: ignore[no-untyped-def]
                path = store.round_dir(0) / "checkpoints" / "s1.json"
                seen.append(json.loads(path.read_text())["state"])
                return await super().request_decision(request)

        solver = FakeSolver([SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH)])
        runner = make_runner(
            solver=solver,
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=CheckpointObservingChannel([EscalationVerdict.ABANDON]),
        )
        await runner.run_attempt(make_problem("s1"), make_config(), output_dir=store.round_dir(0))
        assert seen == [AttemptState.ESCALATED.value]

    async def test_checkpoint_on_disk_reconstructs_a_resumable_attempt(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        solver = FakeSolver([SolverStep(tokens=13)])
        runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempt(make_problem("s1"), make_config(), output_dir=store.round_dir(0))
        body = json.loads((store.round_dir(0) / "checkpoints" / "s1.json").read_text())
        restored = Attempt(
            attempt_id=body["attempt_id"],
            problem_id=body["problem_id"],
            round_id=body["round_id"],
            seed=body["seed"],
            workspace_path=Path(body["workspace_path"]),
            cap=Cap(
                max_steps=body["cap"]["max_steps"],
                max_tokens=body["cap"]["max_tokens"],
                max_wall_clock_seconds=body["cap"]["max_wall_clock_seconds"],
                extension_count=body["cap"]["extension_count"],
            ),
            state=AttemptState(body["state"]),
            step_index=body["step_index"],
            checkpoint_seq=body["checkpoint_seq"],
        )
        assert restored.consumed.steps == 0  # a fresh reader must add consumption
        assert restored.state is AttemptState.FAILED_WITHIN_CAP
        assert body["consumed"]["tokens"] == 13 * DEFAULT_CAP.max_steps

    async def test_a_decision_for_a_different_request_is_refused(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        class WrongIdChannel:
            async def request_decision(self, request):  # type: ignore[no-untyped-def]
                return EscalationDecision(
                    request_id="some-other-request",
                    verdict=EscalationVerdict.CONTINUE,
                    decided_at_ms=1,
                )

        solver = FakeSolver([SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH)])
        runner = make_runner(
            solver=solver,
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=WrongIdChannel(),
        )
        with pytest.raises(EscalationProtocolError, match="does not answer"):
            await runner.run_attempt(
                make_problem("s1"), make_config(), output_dir=store.round_dir(0)
            )

    async def test_escalation_budget_does_not_override_continue(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """CONTINUE means continue. FAILED_WITHIN_CAP requires a tripped cap."""
        solver = FakeSolver([SolverStep(tokens=1, escalate=EscalationReason.REPEATED_REGRESSION)])
        channel = ScriptedEscalationChannel([EscalationVerdict.CONTINUE])
        runner = make_runner(
            solver=solver,
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        cap = Cap(max_steps=5, max_tokens=10_000, max_wall_clock_seconds=600.0)
        outcome = await runner.run_attempt(
            make_problem("s1", default_cap=cap),
            make_config(cap=cap, max_escalations_per_attempt=2),
            output_dir=store.round_dir(0),
        )
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert outcome.attempt.cap_exhausted
        assert outcome.attempt.consumed.steps == cap.max_steps
        assert len(channel.requests) > 2
        assert outcome.attempt.state is not AttemptState.ABANDONED

    async def test_continue_on_a_broken_solver_runs_until_the_cap(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Exceptions cannot self-quit by burning the escalation budget."""
        channel = ScriptedEscalationChannel([EscalationVerdict.CONTINUE])
        runner = make_runner(
            solver=ExplodingSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        cap = Cap(max_steps=1000, max_tokens=100_000, max_wall_clock_seconds=1.0)
        outcome = await runner.run_attempt(
            make_problem("s1", default_cap=cap),
            make_config(cap=cap, max_escalations_per_attempt=2),
            output_dir=store.round_dir(0),
        )
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert outcome.attempt.cap_exhausted
        assert outcome.attempt.consumed.steps == 0
        assert len(channel.requests) > 2


# --------------------------------------------------------------------------- #
# A whole round
# --------------------------------------------------------------------------- #


class TestRound:
    def corpus(self) -> list:
        return [
            make_problem("speed-1", scores=(2.0,)),
            make_problem("speed-2", scores=(3.0,), split=Split.HELD_OUT),
            make_problem("kaggle-1", scores=(0.4,), problem_type=ProblemType.KAGGLE),
        ]

    async def test_round_runs_every_problem_unattended(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        channel = ScriptedEscalationChannel()
        runner = make_runner(
            solver=FakeSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_round(self.corpus(), make_config())
        assert len(outcome.attempts) == 3
        assert channel.requests == []
        assert outcome.record.escalation_count == 0
        assert outcome.record.cost.attempts == 3

    async def test_round_reports_cells_and_never_a_blended_number(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round(self.corpus(), make_config())
        cells = outcome.record.primary_scores()
        assert set(cells) == {
            (ProblemType.SPEEDUP, Split.PRACTICE),
            (ProblemType.SPEEDUP, Split.HELD_OUT),
            (ProblemType.KAGGLE, Split.PRACTICE),
        }
        assert cells[(ProblemType.SPEEDUP, Split.PRACTICE)] == pytest.approx(2.0)
        assert cells[(ProblemType.KAGGLE, Split.PRACTICE)] == pytest.approx(0.4)
        assert not hasattr(outcome.record, "primary")
        assert not hasattr(outcome.record, "mean_score")

    async def test_round_primary_is_the_latest_verify_not_the_max(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        cap = Cap(max_steps=4, max_tokens=10_000, max_wall_clock_seconds=600.0)
        problem = make_problem("s1", scores=(1.2, 2.0, 0.9), default_cap=cap)
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round([problem], make_config(cap=cap))
        cells = outcome.record.primary_scores()
        assert cells[(ProblemType.SPEEDUP, Split.PRACTICE)] == pytest.approx(0.9)

    async def test_verify_every_step_false_does_not_floor_the_round(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """A round that never verified would enter every cell at the scale floor."""
        problem = make_problem("s1", scores=(4.0,))
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round([problem], make_config(verify_every_step=False))
        cells = outcome.record.primary_scores()
        assert cells[(ProblemType.SPEEDUP, Split.PRACTICE)] == pytest.approx(4.0)
        assert all(item.scored for item in outcome.scored)
        assert len(problem.verifier.calls) == 1
        assert outcome.trajectory_row is not None
        assert outcome.trajectory_row["verify_every_step"] is False
        assert outcome.trajectory_row["scored_n"]["speedup/practice"] == 1
        assert outcome.trajectory_row["floored_n"]["speedup/practice"] == 0

    async def test_round_zero_refuses_a_saturation_verdict(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round(self.corpus(), make_config())
        assert outcome.record.deltas == ()
        assert all(a.is_refusal for a in outcome.assessments)
        assert outcome.record.gates["noise_floor_available"] is False

    async def test_round_one_without_a_noise_floor_refuses_too(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        base = await runner.run_round(self.corpus(), make_config())
        second = await runner.run_round(
            self.corpus(),
            make_config(round_index=1, run_id="r01", parent_round_id="r00"),
            parent=base.record,
        )
        assert second.record.deltas == ()
        assert {a.verdict for a in second.assessments} == {SaturationVerdict.REFUSED_NO_NOISE_FLOOR}
        assert "no noise floor measured" in second.record.verdict

    async def test_round_one_with_a_noise_floor_produces_deltas(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        corpus = self.corpus()
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        base = await runner.run_round(corpus, make_config())
        floors = floors_for(corpus, {1: 1.0, 2: 1.1, 3: 1.2})
        improved = [
            make_problem("speed-1", scores=(4.0,)),
            make_problem("speed-2", scores=(3.0,), split=Split.HELD_OUT),
            make_problem("kaggle-1", scores=(0.4,), problem_type=ProblemType.KAGGLE),
        ]
        second = await runner.run_round(
            improved,
            make_config(round_index=1, run_id="r01", parent_round_id="r00"),
            parent=base.record,
            noise_floor=floors,
        )
        delta = second.record.delta_for(ProblemType.SPEEDUP, Split.PRACTICE)
        assert delta is not None
        assert delta.marginal_gain == pytest.approx(2.0)
        assert delta.beats_noise_floor is True
        assert delta.cost_per_unit_gain is not None

    async def test_eval_set_change_is_flagged_and_kills_the_deltas(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        corpus = self.corpus()
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        base = await runner.run_round(corpus, make_config())
        dropped = corpus[:-1]
        second = await runner.run_round(
            dropped,
            make_config(
                round_index=1,
                run_id="r01",
                parent_round_id="r00",
            ),
            parent=base.record,
            noise_floor=floors_for(dropped, {1: 1.0, 2: 1.1, 3: 1.2}),
        )
        assert second.record.deltas == ()
        assert second.record.gates["eval_set_stable"] is False
        assert {a.verdict for a in second.assessments} == {
            SaturationVerdict.REFUSED_EVAL_SET_CHANGED
        }
        assert second.trajectory_row is not None
        assert second.trajectory_row["comparable_to_parent"] is False
        assert second.trajectory_row["trajectory_restart"] is True

    async def test_a_promised_hash_that_does_not_match_the_corpus_is_refused(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Drop a problem and reuse a hash must not look like a comparable round."""
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        with pytest.raises(ContractViolationError, match="does not match the corpus fingerprint"):
            await runner.run_round(self.corpus(), make_config(eval_set_hash="corpus-v1"))

    async def test_a_floor_from_another_eval_set_is_refused(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """A 0.001 floor from an unrelated corpus must not license a 0.5 gain."""
        corpus = self.corpus()
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        base = await runner.run_round(corpus, make_config())
        foreign = floors_for(corpus, {1: 1.0, 2: 1.001, 3: 0.999}, eval_set_hash="other-corpus")
        with pytest.raises(ContractViolationError, match="eval_set_hash"):
            await runner.run_round(
                corpus,
                make_config(round_index=1, run_id="r01", parent_round_id="r00"),
                parent=base.record,
                noise_floor=foreign,
            )

    async def test_a_floor_from_another_engine_is_refused(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        corpus = self.corpus()
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        base = await runner.run_round(corpus, make_config())
        other_engine = EngineIdentity(
            backend="other",
            orchestrator_model="other",
            substep_model=None,
            scaffold_git_sha="deadbeef",
        )
        foreign = floors_for(corpus, {1: 1.0, 2: 1.1, 3: 1.2}, engine=other_engine)
        with pytest.raises(ContractViolationError, match="engine"):
            await runner.run_round(
                corpus,
                make_config(round_index=1, run_id="r01", parent_round_id="r00"),
                parent=base.record,
                noise_floor=foreign,
            )

    async def test_bare_floors_have_no_provenance_and_are_refused(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        corpus = self.corpus()
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        base = await runner.run_round(corpus, make_config())
        with pytest.raises(ContractViolationError, match="provenance"):
            await runner.run_round(
                corpus,
                make_config(round_index=1, run_id="r01", parent_round_id="r00"),
                parent=base.record,
                noise_floor=floors_for(corpus, {1: 1.0, 2: 1.1, 3: 1.2}).floors,  # type: ignore[arg-type]
            )

    async def test_escalations_are_counted_as_human_gate_load(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        solver = FakeSolver([SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH)])
        channel = ScriptedEscalationChannel([EscalationVerdict.ABANDON])
        runner = make_runner(
            solver=solver,
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_round(self.corpus(), make_config())
        assert outcome.record.escalation_count == 3
        assert outcome.trajectory_row is not None
        assert outcome.trajectory_row["human_interventions"] == 3

    async def test_round_wall_clock_excludes_operator_wait(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Driving function #3 must not measure when the operator woke up."""

        class DelayedChannel(ScriptedEscalationChannel):
            async def request_decision(self, request):  # type: ignore[no-untyped-def]
                clock.advance(8 * 3600)
                return await super().request_decision(request)

        solver = FakeSolver([SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH)])
        runner = make_runner(
            solver=solver,
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=DelayedChannel([EscalationVerdict.ABANDON]),
        )
        outcome = await runner.run_round([make_problem("s1")], make_config())
        assert outcome.record.escalation_count == 1
        assert outcome.record.cost.wall_clock_seconds < 60.0

    async def test_an_abandoned_problem_still_enters_its_cell_at_the_floor(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Dropping it would bias the cell upward by the attempts that went worst."""
        solver = FakeSolver([SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH)])
        runner = make_runner(
            solver=solver,
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=ScriptedEscalationChannel([EscalationVerdict.ABANDON]),
        )
        problem = make_problem("s1", raises=True)
        outcome = await runner.run_round([problem], make_config())
        cell = outcome.record.score_for(ProblemType.SPEEDUP, Split.PRACTICE)
        assert cell is not None
        assert cell.n == 1
        assert cell.scores["s1"] == pytest.approx(0.0)
        assert cell.correctness_passes == 0
        assert outcome.scored[0].scored is False
        assert outcome.trajectory_row is not None
        assert outcome.trajectory_row["n"] == {"speedup/practice": 1}
        assert outcome.trajectory_row["floored_n"]["speedup/practice"] == 1
        assert outcome.trajectory_row["scored_n"]["speedup/practice"] == 0
        assert outcome.trajectory_row["seed"] == 7
        assert outcome.trajectory_row["cap"] == {
            "max_steps": 3,
            "max_tokens": 10_000,
            "max_wall_clock_seconds": 600.0,
        }
        assert outcome.trajectory_row["verify_every_step"] is True

    async def test_a_harness_failure_enters_its_cell_unscored(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """A 0.0 from a broken harness must not contaminate the cell as a grade."""
        runner = make_runner(
            solver=FakeSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=ScriptedEscalationChannel([EscalationVerdict.ABANDON]),
        )
        problem = make_problem("s1", harness_failure=True)
        outcome = await runner.run_round([problem], make_config())
        cell = outcome.record.score_for(ProblemType.SPEEDUP, Split.PRACTICE)
        assert cell is not None
        assert cell.n == 1
        assert cell.scores["s1"] == pytest.approx(0.0)
        assert cell.correctness_passes == 0
        assert outcome.scored[0].scored is False
        assert outcome.attempts[0].best_result is None

    async def test_an_empty_corpus_is_refused(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        with pytest.raises(ContractViolationError, match="measures nothing"):
            await runner.run_round([], make_config())

    async def test_config_requires_lineage_past_round_zero(self) -> None:
        with pytest.raises(ContractViolationError, match="must record its parent"):
            make_config(round_index=1, run_id="r01", parent_round_id=None)
        with pytest.raises(ContractViolationError, match="no parent"):
            make_config(round_index=0, parent_round_id="r00")

    async def test_round_artifacts_land_in_the_prescribed_layout(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_round(self.corpus(), make_config())
        round_dir = store.round_dir(0)
        assert round_dir.name == "round-00"
        assert (round_dir / "round.json").exists()
        assert (round_dir / "attempts" / "speed-1.json").exists()
        assert (round_dir / "checkpoints" / "speed-1.json").exists()
        assert store.trajectory_path.exists()

    async def test_attempt_log_records_the_step_trajectory(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(
            solver=FakeSolver([SolverStep(tokens=7, note="tried a thing")]),
            store=store,
            workspaces=workspaces,
            clock=clock,
        )
        await runner.run_round([make_problem("s1", scores=(2.0,))], make_config())
        body = json.loads((store.round_dir(0) / "attempts" / "s1.json").read_text())
        assert body["problem_id"] == "s1"
        assert body["seed"] == 7
        assert len(body["steps"]) == DEFAULT_CAP.max_steps
        assert body["steps"][0]["note"] == "tried a thing"
        assert body["steps"][0]["score"] == pytest.approx(2.0)
        assert body["best_score"] == pytest.approx(2.0)


class TestRunnerType:
    def test_runner_exposes_its_clock(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        assert isinstance(runner, RoundRunner)
        assert runner.clock is clock


class TestLineageGuards:
    async def test_a_parent_that_is_not_the_recorded_parent_is_refused(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Deltas that disagree with the recorded lineage are worse than none."""
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        base = await runner.run_round([make_problem("s1")], make_config(run_id="r00"))
        with pytest.raises(ContractViolationError, match="lineage disagrees"):
            await runner.run_round(
                [make_problem("s1")],
                make_config(round_index=1, run_id="r01", parent_round_id="some-other-round"),
                parent=base.record,
            )

    def test_a_criterion_that_is_no_bar_at_all_is_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="omit the criterion"):
            PassCriterion(min_score=None, require_correctness=False)
