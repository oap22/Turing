"""Resume: a restart must not re-drive work that is already on disk (RES-17).

The sweep runs on an opportunistic subscription that closes at unpredictable
points, so "the process died mid-round" is the normal case, not the exotic one.
RES-12 made a re-drive *safe* — ``MetricsWriter`` refuses to splice onto an
existing chain and the call site rotates the prior generation into ``prior-N/``
— but not *correct*: a completed attempt re-run from scratch burns scarce
compute to re-measure something already measured.

Every test here drives the real :class:`RoundRunner` and
:class:`NoiseFloorRunner` against the package's own fakes. A restart is
simulated the way a killed process actually behaves: a ``BaseException``
(``KeyboardInterrupt``) out of the solver or the escalation channel, which
``run_attempts``' ``except Exception`` containment deliberately does not catch.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import (
    Attempt,
    AttemptState,
    CapConsumption,
    ContractViolationError,
    EscalationReason,
    EscalationVerdict,
)
from turing.research.loop.noise_floor import NoiseFloorConfig, NoiseFloorRunner
from turing.research.loop.protocols import SolverStep
from turing.research.loop.trajectory import AttemptDisposition, RunIdentity, cell_key
from turing.research.problems.adapter import bind_eval_set_hash

from .conftest import (
    DEFAULT_CAP,
    ENGINE,
    FakeClock,
    FakeSolver,
    ScriptedEscalationChannel,
    TempWorkspaceProvider,
    WorkspacesRefusingOneProblem,
    make_config,
    make_problem,
    make_runner,
)

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import Attempt as AttemptType
    from turing.research.contracts import EscalationDecision, EscalationRequest
    from turing.research.loop.protocols import SolverTask
    from turing.research.loop.trajectory import TrajectoryStore


# --------------------------------------------------------------------------- #
# Fakes that stand in for "the process died here"
# --------------------------------------------------------------------------- #


class KilledSolver:
    """Works normally until the (k+1)-th distinct problem, then dies.

    ``KeyboardInterrupt`` rather than an ordinary exception on purpose:
    ``run_attempts`` contains an ``Exception`` to one attempt and keeps going,
    which is the *opposite* of what a closed subscription window does. A
    ``BaseException`` propagates out of the round exactly as a kill signal
    would.
    """

    def __init__(self, die_after_problems: int) -> None:
        self._die_after = die_after_problems
        self.seen: list[str] = []
        self.calls: list[tuple[str, int]] = []

    async def step(self, task: SolverTask, attempt: AttemptType) -> SolverStep:
        if task.id not in self.seen:
            self.seen.append(task.id)
        if len(self.seen) > self._die_after:
            raise KeyboardInterrupt("subscription window closed")
        self.calls.append((task.id, attempt.step_index))
        return SolverStep(tokens=10, note="work")


class SeedRecordingSolver:
    """Records which seed each step was driven for."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def step(self, task: SolverTask, attempt: AttemptType) -> SolverStep:
        self.calls.append((attempt.seed, task.id))
        return SolverStep(tokens=10, note="work")


class DyingChannel:
    """Publishes the escalation, then the process dies waiting for the answer."""

    def __init__(self) -> None:
        self.requests: list[EscalationRequest] = []

    async def request_decision(self, request: EscalationRequest) -> EscalationDecision:
        self.requests.append(request)
        raise KeyboardInterrupt("subscription window closed while waiting on the operator")


def _corpus(count: int) -> list:
    return [make_problem(f"s{i}", scores=(float(i),)) for i in range(1, count + 1)]


# --------------------------------------------------------------------------- #
# A round killed mid-way
# --------------------------------------------------------------------------- #


class TestARestartMidRoundDoesNotRedriveCompletedProblems:
    async def test_only_the_unfinished_problems_are_driven_again(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        corpus = _corpus(4)
        config = make_config()
        killed = KilledSolver(die_after_problems=2)
        first = make_runner(solver=killed, store=store, workspaces=workspaces, clock=clock)
        with pytest.raises(KeyboardInterrupt):
            await first.run_round(corpus, config)

        second_solver = FakeSolver()
        second = make_runner(solver=second_solver, store=store, workspaces=workspaces, clock=clock)
        outcome = await second.run_round(corpus, config)

        assert {problem_id for problem_id, _ in second_solver.calls} == {"s3", "s4"}
        assert second.skipped_problems == ("s1", "s2")
        assert len(outcome.attempts) == 4
        assert outcome.record.gates["all_attempts_completed"] is True

    async def test_the_round_record_is_written_once_and_covers_every_problem(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        corpus = _corpus(4)
        config = make_config()
        first = make_runner(
            solver=KilledSolver(die_after_problems=2),
            store=store,
            workspaces=workspaces,
            clock=clock,
        )
        with pytest.raises(KeyboardInterrupt):
            await first.run_round(corpus, config)
        assert (await store.load_trajectory())["rounds"] == []

        second = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await second.run_round(corpus, config)

        rows = (await store.load_trajectory())["rounds"]
        assert len(rows) == 1
        assert rows[0]["n"] == {"speedup/practice": 4}
        assert rows[0]["constraints"]["all_attempts_completed"] is True

    async def test_a_reused_outcome_keeps_the_score_the_first_process_measured(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """The skip reuses a measurement; it does not floor the problem."""
        corpus = _corpus(3)
        config = make_config()
        first = make_runner(
            solver=KilledSolver(die_after_problems=2),
            store=store,
            workspaces=workspaces,
            clock=clock,
        )
        with pytest.raises(KeyboardInterrupt):
            await first.run_round(corpus, config)

        second = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await second.run_round(corpus, config)

        by_id = {s.problem_id: s for s in outcome.scored}
        assert by_id["s1"].score == 1.0
        assert by_id["s2"].score == 2.0
        assert all(s.scored for s in outcome.scored)


# --------------------------------------------------------------------------- #
# The completeness table
# --------------------------------------------------------------------------- #


class TestWhatCountsAsComplete:
    async def test_an_abandoned_attempt_is_complete_and_is_not_re_driven(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """An operator verdict is a decision, not an interruption."""
        config = make_config()
        problem = make_problem("s1")
        first = make_runner(
            solver=FakeSolver([SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH)]),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=ScriptedEscalationChannel([EscalationVerdict.ABANDON]),
        )
        first_outcomes = await first.run_attempts([problem], config, output_dir=store.round_dir(0))
        assert first_outcomes[0].attempt.state is AttemptState.ABANDONED

        solver = FakeSolver()
        second = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        outcomes = await second.run_attempts([problem], config, output_dir=store.round_dir(0))

        assert solver.calls == []
        assert second.skipped_problems == ("s1",)
        assert outcomes[0].attempt.state is AttemptState.ABANDONED

    async def test_a_terminal_attempt_with_no_summary_is_not_complete(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """The shape a killed process leaves: intact chain, no ``metrics.json``.

        ``verify`` calls that directory INCOMPLETE, and this must agree with
        it — otherwise the resume path would bless a record the pre-writeup
        gate refuses.
        """
        config = make_config()
        problem = make_problem("s1")
        first = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await first.run_attempts([problem], config, output_dir=store.round_dir(0))
        (store.round_dir(0) / "attempts" / "s1" / "metrics.json").unlink()

        solver = FakeSolver()
        second = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        await second.run_attempts([problem], config, output_dir=store.round_dir(0))

        assert solver.calls != []
        assert second.skipped_problems == ()

    async def test_a_different_generation_is_re_driven_not_reused(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """A re-drive under a *different* run id is not a resume.

        Resume reuses this run's own work. A round or seed run under a new
        ``run_id`` / ``seed`` is a new measurement, and adopting the previous
        generation's numbers for it would be a fabricated result.
        """
        problem = make_problem("s1")
        first = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await first.run_attempts(
            [problem], make_config(run_id="gen1", seed=7), output_dir=store.round_dir(0)
        )

        solver = FakeSolver()
        second = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        await second.run_attempts(
            [problem], make_config(run_id="gen2", seed=99), output_dir=store.round_dir(0)
        )

        assert solver.calls != []
        assert second.skipped_problems == ()

    async def test_the_store_reports_the_disposition_it_used(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        config = make_config()
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempts([make_problem("s1")], config, output_dir=store.round_dir(0))

        identity = RunIdentity(
            round_id=config.run_id, seed=config.seed, eval_set_hash=config.eval_set_hash
        )
        stored = await store.load_completed_attempt(0, "s1", identity=identity)
        assert stored is not None
        assert stored.disposition is AttemptDisposition.COMPLETE
        assert stored.attempt is not None
        assert stored.attempt.state is AttemptState.FAILED_WITHIN_CAP

        foreign = await store.load_completed_attempt(
            0, "s1", identity=RunIdentity(round_id="other", seed=1, eval_set_hash="")
        )
        assert foreign is None


# --------------------------------------------------------------------------- #
# PAUSED — the load-bearing case
# --------------------------------------------------------------------------- #


class TestAPausedAttemptIsResumed:
    async def test_the_checkpoint_is_consumed_and_the_attempt_id_survives(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        tmp_path: Path,
    ) -> None:
        config = make_config()
        problem = make_problem("s1", scores=(4.0,))
        workspace = tmp_path / "already-materialised"
        workspace.mkdir(parents=True)
        paused = Attempt(
            attempt_id="preserved-attempt-id",
            problem_id="s1",
            round_id=config.run_id,
            seed=config.seed,
            workspace_path=workspace,
            cap=DEFAULT_CAP,
            consumed=CapConsumption(steps=2, tokens=20, wall_clock_seconds=1.0),
            state=AttemptState.PAUSED,
            step_index=2,
            checkpoint_seq=5,
            resume_token="backend-token-7",
            started_at_ms=1_700_000_000_000,
            updated_at_ms=1_700_000_000_000,
        )
        await store.write_attempt_checkpoint(
            paused, output_dir=store.round_dir(0), eval_set_hash=config.eval_set_hash
        )

        solver = FakeSolver([SolverStep(tokens=5)])
        runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        outcomes = await runner.run_attempts([problem], config, output_dir=store.round_dir(0))

        attempt = outcomes[0].attempt
        assert attempt.attempt_id == "preserved-attempt-id"
        assert attempt.workspace_path == workspace
        # 2 steps were already spent; the cap is 3, so exactly one more runs.
        assert len(solver.calls) == 1
        assert attempt.consumed.steps == 3
        assert attempt.step_index == 3
        assert attempt.state is AttemptState.FAILED_WITHIN_CAP
        # Nothing was re-materialised: the partly-solved workspace is the work.
        assert workspaces.made == []


# --------------------------------------------------------------------------- #
# ESCALATED — re-enter the wait, do not re-drive
# --------------------------------------------------------------------------- #


class TestAnOpenEscalationIsReopenedNotRestarted:
    async def test_the_restart_waits_on_the_same_request(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        config = make_config()
        problem = make_problem("s1")
        dying = DyingChannel()
        first = make_runner(
            solver=FakeSolver([SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH)]),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=dying,
        )
        with pytest.raises(KeyboardInterrupt):
            await first.run_attempts([problem], config, output_dir=store.round_dir(0))
        open_request = dying.requests[0]

        channel = ScriptedEscalationChannel([EscalationVerdict.ABANDON])
        solver = FakeSolver()
        second = make_runner(
            solver=solver,
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcomes = await second.run_attempts([problem], config, output_dir=store.round_dir(0))

        assert [r.request_id for r in channel.requests] == [open_request.request_id]
        assert solver.calls == []  # abandoned on the reopened decision, never re-driven
        assert outcomes[0].attempt.attempt_id == open_request.attempt_id
        assert outcomes[0].attempt.state is AttemptState.ABANDONED
        # The escalation still counts once toward human-gate load.
        assert outcomes[0].escalation_count == 1


# --------------------------------------------------------------------------- #
# Noise floor
# --------------------------------------------------------------------------- #


class TestTheNoiseFloorSkipsCompleteSeeds:
    @staticmethod
    def _config() -> NoiseFloorConfig:
        return NoiseFloorConfig(
            run_id="nf",
            eval_set_hash="",
            engine=ENGINE,
            seeds=(1, 2, 3),
            default_cap=DEFAULT_CAP,
        )

    async def test_a_complete_seed_is_not_driven_again_and_still_enters_the_floor(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        corpus = [make_problem("s1", scores=(1.0,)), make_problem("s2", scores=(2.0,))]
        config = self._config()
        eval_set_hash = bind_eval_set_hash(corpus, config.eval_set_hash)

        pre = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await pre.run_attempts(
            corpus,
            replace(config.round_config_for(2), eval_set_hash=eval_set_hash),
            output_dir=store.noise_floor_seed_dir(2),
        )

        solver = SeedRecordingSolver()
        runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        floor_runner = NoiseFloorRunner(runner, store)
        report = await floor_runner.run(corpus, config)

        assert {seed for seed, _ in solver.calls} == {1, 3}
        assert floor_runner.skipped_seeds == (2,)
        assert set(report.per_seed_scores) == {1, 2, 3}
        assert await store.has_noise_floor()

    async def test_an_incomplete_seed_is_still_refused(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """The refusal semantics survive the skip path.

        A seed that is *genuinely* incomplete — it lost an attempt — still
        gets no floor written, even when a sibling seed was skipped as
        already-complete.
        """
        corpus = [make_problem("s1", scores=(1.0,)), make_problem("s2", scores=(2.0,))]
        config = self._config()
        eval_set_hash = bind_eval_set_hash(corpus, config.eval_set_hash)

        pre = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await pre.run_attempts(
            corpus,
            replace(config.round_config_for(2), eval_set_hash=eval_set_hash),
            output_dir=store.noise_floor_seed_dir(2),
        )

        refusing = WorkspacesRefusingOneProblem(workspaces.root, problem_ids=["s2"])
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=refusing, clock=clock)
        with pytest.raises(ContractViolationError, match="lost 1 of 2 attempt"):
            await NoiseFloorRunner(runner, store).run(corpus, config)
        assert not await store.has_noise_floor()

    async def test_list_completed_seeds_names_only_the_finished_ones(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        corpus = [make_problem("s1"), make_problem("s2")]
        config = self._config()
        eval_set_hash = bind_eval_set_hash(corpus, config.eval_set_hash)
        pre = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await pre.run_attempts(
            corpus,
            replace(config.round_config_for(2), eval_set_hash=eval_set_hash),
            output_dir=store.noise_floor_seed_dir(2),
        )

        identities = {
            seed: RunIdentity(
                round_id=config.round_config_for(seed).run_id,
                seed=seed,
                eval_set_hash=eval_set_hash,
            )
            for seed in config.seeds
        }
        assert await store.list_completed_seeds(identities, ["s1", "s2"]) == (2,)


# --------------------------------------------------------------------------- #
# The driver's own sequence, end to end
# --------------------------------------------------------------------------- #


class TestTheFloorThenRoundZeroSequenceSurvivesARestart:
    """What ``python -m turing.research.loop.run`` does, driven with fakes.

    The driver measures the floor and then round 0, binding the report to the
    round. This is the sequence a closed subscription window actually
    interrupts, and the property that matters is that re-running the identical
    command finishes it without re-measuring anything already measured — the
    floor included.
    """

    async def test_a_restart_re_derives_the_floor_and_finishes_the_round(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        corpus = [make_problem("s1", scores=(1.0,)), make_problem("s2", scores=(2.0,))]
        floor_config = NoiseFloorConfig(
            run_id="nf",
            eval_set_hash="",
            engine=ENGINE,
            seeds=(1, 2, 3),
            default_cap=DEFAULT_CAP,
        )
        round_config = make_config(run_id="r00", seed=1)

        first_floor = NoiseFloorRunner(
            make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock),
            store,
        )
        report = await first_floor.run(corpus, floor_config)
        assert first_floor.skipped_seeds == ()

        killed = KilledSolver(die_after_problems=1)
        with pytest.raises(KeyboardInterrupt):
            await make_runner(
                solver=killed, store=store, workspaces=workspaces, clock=clock
            ).run_round(corpus, round_config, noise_floor=report)

        # The restart: the identical sequence, over the same results tree.
        second_floor = NoiseFloorRunner(
            make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock),
            store,
        )
        re_derived = await second_floor.run(corpus, floor_config)
        assert second_floor.skipped_seeds == (1, 2, 3)
        assert {cell_key(f.cell): f.value for f in re_derived.floors} == {
            cell_key(f.cell): f.value for f in report.floors
        }

        solver = FakeSolver()
        round_runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        outcome = await round_runner.run_round(corpus, round_config, noise_floor=re_derived)

        assert {problem_id for problem_id, _ in solver.calls} == {"s2"}
        assert round_runner.skipped_problems == ("s1",)
        rows = (await store.load_trajectory())["rounds"]
        assert len(rows) == 1
        assert rows[0]["noise_floor_measured"] is True
        assert outcome.record.gates["noise_floor_available"] is True
