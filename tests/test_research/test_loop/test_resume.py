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

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import (
    SCORE_SCALE_SPEEDUP,
    Attempt,
    AttemptState,
    Cap,
    CapConsumption,
    ContractViolationError,
    EscalationReason,
    EscalationVerdict,
)
from turing.research.loop.escalation import (
    FileDropDecisionInbox,
    NtfyEscalationNotifier,
    OperatorEscalationChannel,
)
from turing.research.loop.metrics import DEFAULT_SCORE_FLOORS
from turing.research.loop.noise_floor import NoiseFloorConfig, NoiseFloorRunner
from turing.research.loop.protocols import SolverStep
from turing.research.loop.runner import PassCriterion, RoundConfig
from turing.research.loop.trajectory import (
    AttemptDisposition,
    RunIdentity,
    StoredAttempt,
    cell_key,
)
from turing.research.problems.adapter import bind_eval_set_hash

from .conftest import (
    DEFAULT_CAP,
    ENGINE,
    FakeClock,
    FakeSolver,
    RecordingNtfyClient,
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

    async def request_decision(
        self, request: EscalationRequest, *, reopened: bool = False
    ) -> EscalationDecision:
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

        second_solver = FakeSolver()
        second = make_runner(solver=second_solver, store=store, workspaces=workspaces, clock=clock)
        outcome = await second.run_round(corpus, config)

        # Exactly the two unfinished problems were driven, to their 3-step cap
        # each; the two finished ones came off disk.
        assert second.skipped_problems == ("s1", "s2")
        assert second.resumed_problems == ()
        assert [problem_id for problem_id, _ in second_solver.calls] == ["s3"] * 3 + ["s4"] * 3
        assert outcome.record.cost.attempts == 4
        rows = (await store.load_trajectory())["rounds"]
        assert len(rows) == 1
        assert rows[0]["n"] == {"speedup/practice": 4}
        assert rows[0]["constraints"]["all_attempts_completed"] is True
        assert (store.round_dir(0) / "round.json").exists()

    async def test_a_reused_outcome_keeps_the_score_the_first_process_measured(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """The skip reuses a measurement; it does not floor the problem, and it
        does not re-measure it either.

        The restart's corpus carries verifiers that would score every problem
        ten times higher, so a re-drive of ``s1`` or ``s2`` would be visible
        as 10.0 / 20.0. The reused cells read 1.0 / 2.0 — the first process's
        numbers — while ``s3``, which *is* driven, reads the new 30.0.
        """
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

        restart_corpus = [make_problem(f"s{i}", scores=(10.0 * i,)) for i in range(1, 4)]
        second = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await second.run_round(restart_corpus, config)

        by_id = {s.problem_id: s for s in outcome.scored}
        assert by_id["s1"].score == 1.0
        assert by_id["s2"].score == 2.0
        assert by_id["s3"].score == 30.0
        assert all(s.scored for s in outcome.scored)
        assert second.skipped_problems == ("s1", "s2")


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

        stored = await store.load_completed_attempt(0, "s1", identity=config.identity())
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
            paused,
            output_dir=store.round_dir(0),
            eval_set_hash=config.eval_set_hash,
            config_digest=config.config_digest(),
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
            seed: replace(config.round_config_for(seed), eval_set_hash=eval_set_hash).identity()
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


# --------------------------------------------------------------------------- #
# The contract, restated as tests (adversarial review of RES-17)
#
# "Resume never adopts a number this run did not measure under this exact
# configuration, never destroys a measurement, and re-running a finished sweep
# with the identical command is a no-op success."
# --------------------------------------------------------------------------- #


class DieOnThirdStep:
    """Two steps land (checkpoint + log after each), then the window closes."""

    def __init__(self) -> None:
        self.n = 0
        self.calls: list[tuple[str, int]] = []

    async def step(self, task: SolverTask, attempt: AttemptType) -> SolverStep:
        self.n += 1
        if self.n > 2:
            raise KeyboardInterrupt("window closed")
        self.calls.append((task.id, attempt.step_index))
        return SolverStep(tokens=10, note="work")


def _paused_attempt(
    *,
    config: RoundConfig,
    workspace: Path,
    attempt_id: str = "preserved-id",
    state: AttemptState = AttemptState.PAUSED,
    escalation_id: str | None = None,
    problem_id: str = "s1",
) -> Attempt:
    return Attempt(
        attempt_id=attempt_id,
        problem_id=problem_id,
        round_id=config.run_id,
        seed=config.seed,
        workspace_path=workspace,
        cap=DEFAULT_CAP,
        consumed=CapConsumption(steps=1, tokens=10, wall_clock_seconds=1.0),
        state=state,
        step_index=1,
        checkpoint_seq=3,
        started_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_000_000,
        escalation_id=escalation_id,
    )


async def _write_checkpoint(
    store: TrajectoryStore, attempt: Attempt, config: RoundConfig, *, output_dir: Path
) -> Path:
    return await store.write_attempt_checkpoint(
        attempt,
        output_dir=output_dir,
        eval_set_hash=config.eval_set_hash,
        config_digest=config.config_digest(),
    )


class TestARestartAfterTheRoundFinishedIsANoOp:
    """Finding 1: the documented procedure must not end in a refusal.

    Before the fix ``run_round`` rewrote ``round.json`` (new cost, new
    ``created_at_ms``) and *then* ``append_round`` refused — every restart
    after completion exited 2 forever, having damaged the record.
    """

    async def test_the_identical_command_returns_the_first_record_and_writes_nothing(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        corpus = _corpus(2)
        config = make_config()
        first_solver = FakeSolver()
        first = make_runner(solver=first_solver, store=store, workspaces=workspaces, clock=clock)
        first_outcome = await first.run_round(corpus, config)
        record_path = store.round_dir(0) / "round.json"
        record_before = record_path.read_bytes()
        trajectory_before = store.trajectory_path.read_bytes()

        solver = FakeSolver()
        second = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        outcome = await second.run_round(corpus, config)

        assert outcome.already_finished is True
        assert solver.calls == []
        assert outcome.record == first_outcome.record
        assert outcome.record.created_at_ms == first_outcome.record.created_at_ms
        assert outcome.record.cost == first_outcome.record.cost
        assert {s.problem_id: s.score for s in outcome.scored} == {"s1": 1.0, "s2": 2.0}
        assert outcome.trajectory_row == first_outcome.trajectory_row
        # Nothing on disk moved.
        assert record_path.read_bytes() == record_before
        assert store.trajectory_path.read_bytes() == trajectory_before
        assert len((await store.load_trajectory())["rounds"]) == 1
        # And a third restart is the same no-op.
        third = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        assert (await third.run_round(corpus, config)).already_finished is True

    async def test_a_different_run_id_under_a_taken_index_is_refused_before_any_write(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        corpus = _corpus(2)
        first = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await first.run_round(corpus, make_config(run_id="r00"))
        record_path = store.round_dir(0) / "round.json"
        before = record_path.read_bytes()

        solver = FakeSolver()
        second = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        with pytest.raises(ContractViolationError, match=r"already in .*nothing was written"):
            await second.run_round(corpus, make_config(run_id="r00-remeasured"))

        assert solver.calls == []
        assert record_path.read_bytes() == before
        assert len((await store.load_trajectory())["rounds"]) == 1

    async def test_round_json_is_written_after_the_trajectory_row(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """A refused append can no longer have already rewritten the record."""
        corpus = _corpus(1)
        config = make_config()
        order: list[str] = []
        original_append = store.append_round
        original_write = store.write_round_record

        async def spy_append(*args: object, **kwargs: object) -> dict:  # type: ignore[type-arg]
            order.append("append_round")
            return await original_append(*args, **kwargs)  # type: ignore[arg-type]

        async def spy_write(*args: object, **kwargs: object) -> Path:
            order.append("write_round_record")
            return await original_write(*args, **kwargs)  # type: ignore[arg-type]

        store.append_round = spy_append  # type: ignore[method-assign]
        store.write_round_record = spy_write  # type: ignore[method-assign]
        await make_runner(
            solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock
        ).run_round(corpus, config)
        assert order == ["append_round", "write_round_record"]


class TestTheNoiseFloorIsBoundToTheEngineItWasMeasuredUnder:
    """Finding 2: a floor from scaffold A is not re-derived and stamped B."""

    async def test_a_floor_measured_under_another_engine_is_re_driven_not_restamped(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        corpus = [make_problem("s1", scores=(1.0,)), make_problem("s2", scores=(2.0,))]
        cfg_old = NoiseFloorConfig(
            run_id="nf", eval_set_hash="", engine=ENGINE, seeds=(1, 2, 3), default_cap=DEFAULT_CAP
        )
        await NoiseFloorRunner(
            make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock),
            store,
        ).run(corpus, cfg_old)

        new_engine = replace(ENGINE, scaffold_git_sha="deadbee")  # the self-edit landed
        solver = SeedRecordingSolver()
        second = NoiseFloorRunner(
            make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock), store
        )
        report = await second.run(corpus, replace(cfg_old, engine=new_engine))

        assert second.skipped_seeds == ()
        assert {seed for seed, _ in solver.calls} == {1, 2, 3}
        assert report.engine == new_engine  # now truthfully: every seed was measured under it

    async def test_the_same_engine_is_still_reused_whole(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """The positive control: identity must not have become 'always re-drive'."""
        corpus = [make_problem("s1", scores=(1.0,)), make_problem("s2", scores=(2.0,))]
        cfg = NoiseFloorConfig(
            run_id="nf", eval_set_hash="", engine=ENGINE, seeds=(1, 2, 3), default_cap=DEFAULT_CAP
        )
        await NoiseFloorRunner(
            make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock),
            store,
        ).run(corpus, cfg)
        solver = FakeSolver()
        second = NoiseFloorRunner(
            make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock), store
        )
        await second.run(corpus, cfg)
        assert second.skipped_seeds == (1, 2, 3)
        assert solver.calls == []


class TestConfigDriftReDrivesTheStoredAttempts:
    """Finding 3: identity covers *how* the measurement was taken, not only which."""

    @pytest.mark.parametrize(
        "mutation",
        [
            pytest.param(
                {"default_cap": Cap(max_steps=6, max_tokens=10_000, max_wall_clock_seconds=600.0)},
                id="default_cap",
            ),
            pytest.param(
                {"pass_criteria": {"s1": PassCriterion(min_score=99.0)}}, id="pass_criteria"
            ),
            pytest.param({"verify_every_step": False}, id="verify_every_step"),
            pytest.param({"escalate_on_cap_exhaustion": True}, id="escalate_on_cap_exhaustion"),
            pytest.param({"max_escalations_per_attempt": 5}, id="max_escalations_per_attempt"),
            pytest.param(
                {"score_floors": {**DEFAULT_SCORE_FLOORS, SCORE_SCALE_SPEEDUP: 0.5}},
                id="score_floors",
            ),
            pytest.param({"engine": replace(ENGINE, scaffold_git_sha="deadbee")}, id="engine"),
        ],
    )
    async def test_a_changed_measurement_field_re_drives_a_finished_attempt(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        mutation: dict,  # type: ignore[type-arg]
    ) -> None:
        problem = make_problem("s1")
        config = make_config()
        first = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await first.run_attempts([problem], config, output_dir=store.round_dir(0))

        solver = FakeSolver()
        second = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        await second.run_attempts(
            [problem], replace(config, **mutation), output_dir=store.round_dir(0)
        )
        assert solver.calls != [], f"{mutation} was ignored by RunIdentity"
        assert second.skipped_problems == ()

    def test_the_digest_is_stable_across_equal_configs_and_sensitive_to_each_field(self) -> None:
        base = make_config()
        assert base.config_digest() == make_config().config_digest()
        assert len(base.config_digest()) == 64
        # Not sensitive to identity/lineage coordinates — those are the other
        # three fields of RunIdentity, and folding them in would only hide
        # which coordinate moved.
        assert base.config_digest() == make_config(run_id="other", seed=99).config_digest()
        assert base.config_digest() == make_config(eval_set_hash="a" * 64).config_digest()
        seen = {base.config_digest()}
        for mutation in (
            {"default_cap": Cap(max_steps=6, max_tokens=10_000, max_wall_clock_seconds=600.0)},
            {"pass_criteria": {"s1": PassCriterion(min_score=99.0)}},
            {"verify_every_step": False},
            {"escalate_on_cap_exhaustion": True},
            {"max_escalations_per_attempt": 5},
            {"score_floors": {**DEFAULT_SCORE_FLOORS, SCORE_SCALE_SPEEDUP: 0.5}},
            {"engine": replace(ENGINE, scaffold_git_sha="deadbee")},
            {"engine": replace(ENGINE, orchestrator_model="sonnet")},
        ):
            digest = replace(base, **mutation).config_digest()  # type: ignore[arg-type]
            assert digest not in seen, f"{mutation} did not change the digest"
            seen.add(digest)

    async def test_a_round_restarted_with_a_raised_cap_is_not_half_measured_at_each(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """G2: cap 2 → killed → cap 6 must not yield one record measured two ways."""
        corpus = _corpus(4)
        small = make_config(cap=Cap(max_steps=2, max_tokens=10_000, max_wall_clock_seconds=600.0))
        with pytest.raises(KeyboardInterrupt):
            await make_runner(
                solver=KilledSolver(die_after_problems=2),
                store=store,
                workspaces=workspaces,
                clock=clock,
            ).run_round(corpus, small)

        big = replace(
            small, default_cap=Cap(max_steps=6, max_tokens=10_000, max_wall_clock_seconds=600.0)
        )
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round(corpus, big)
        caps = {o.problem.id: o.attempt.cap.max_steps for o in outcome.attempts}
        assert set(caps.values()) == {6}, f"mixed caps in one round: {caps}"
        assert runner.skipped_problems == ()

    async def test_the_same_config_is_still_reused(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """Positive control for the digest: equal configs built twice still match."""
        problem = make_problem("s1")
        first = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await first.run_attempts([problem], make_config(), output_dir=store.round_dir(0))
        solver = FakeSolver()
        second = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        await second.run_attempts([problem], make_config(), output_dir=store.round_dir(0))
        assert solver.calls == []
        assert second.skipped_problems == ("s1",)


class TestAResumedAttemptKeepsItsEarlierSteps:
    """Finding 4: the attempt log spans the interruption."""

    async def test_the_log_rows_equal_consumed_steps_after_a_resume(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        problem = make_problem("s1", scores=(1.0,), default_cap=Cap(4, 10_000, 600.0))
        config = make_config()
        with pytest.raises(KeyboardInterrupt):
            await make_runner(
                solver=DieOnThirdStep(), store=store, workspaces=workspaces, clock=clock
            ).run_attempts([problem], config, output_dir=store.round_dir(0))
        log_path = store.round_dir(0) / "attempts" / "s1.json"
        # The kill left the two finished steps on disk.
        interrupted = json.loads(log_path.read_text())
        assert [s["index"] for s in interrupted["steps"]] == [1, 2]
        assert interrupted["final_state"] != "failed_within_cap"

        second = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcomes = await second.run_attempts([problem], config, output_dir=store.round_dir(0))
        assert second.resumed_problems == ("s1",)

        log = json.loads(log_path.read_text())
        assert outcomes[0].attempt.consumed.steps == 4
        assert log["consumed"]["steps"] == 4
        assert [s["index"] for s in log["steps"]] == [1, 2, 3, 4]
        assert len(outcomes[0].steps) == 4
        assert log["final_state"] == "failed_within_cap"

        # A further restart reuses it, steps included.
        third = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcomes3 = await third.run_attempts([problem], config, output_dir=store.round_dir(0))
        assert third.skipped_problems == ("s1",)
        assert len(outcomes3[0].steps) == 4


class TestAResumeWithoutItsWorkspaceRefuses:
    """Finding 5: never drive — or verify — against a directory that is not there."""

    async def test_a_missing_workspace_loses_the_problem_loudly_and_measures_nothing(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        tmp_path: Path,
    ) -> None:
        config = make_config()
        broken = make_problem("s1", scores=(4.0,))
        fine = make_problem("s2", scores=(2.0,))
        gone = tmp_path / "turing-workspace" / "preserved-id"  # never created
        checkpoint = await _write_checkpoint(
            store,
            _paused_attempt(config=config, workspace=gone),
            config,
            output_dir=store.round_dir(0),
        )

        solver = FakeSolver([SolverStep(tokens=5)])
        runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        outcomes = await runner.run_attempts([broken, fine], config, output_dir=store.round_dir(0))

        assert [o.problem.id for o in outcomes] == ["s2"]
        assert [f.problem_id for f in runner.attempt_failures] == ["s1"]
        error = runner.attempt_failures[0].error
        assert "ContractViolationError" in error
        assert str(gone) in error
        assert str(checkpoint) in error  # the remedy names the file to delete
        assert "restore the workspace" in error
        # Neither the solver nor s1's verifier ever ran against the missing dir.
        assert {problem_id for problem_id, _ in solver.calls} == {"s2"}
        assert broken.verifier.calls == []  # type: ignore[attr-defined]
        assert not gone.exists()


class TestAnEscalatedCheckpointWithAnUnusableRequestIsReDriven:
    """Finding 6: a request file that exists but cannot be honoured is ABSENT, not fatal."""

    async def _escalated_checkpoint(
        self, store: TrajectoryStore, config: RoundConfig, tmp_path: Path
    ) -> Path:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        await _write_checkpoint(
            store,
            _paused_attempt(
                config=config,
                workspace=workspace,
                state=AttemptState.ESCALATED,
                escalation_id="E1",
            ),
            config,
            output_dir=store.round_dir(0),
        )
        request_path = store.round_dir(0) / "escalations" / "E1.json"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        return request_path

    async def test_a_request_that_does_not_decode_drives_the_problem_again(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        tmp_path: Path,
    ) -> None:
        config = make_config()
        request_path = await self._escalated_checkpoint(store, config, tmp_path)
        # Present, readable JSON, missing the fields the rebuild requires.
        request_path.write_text(
            json.dumps({"request": {"request_id": "E1", "attempt_id": "preserved-id"}}),
            encoding="utf-8",
        )

        stored = await store.load_stored_attempt(
            "s1", output_dir=store.round_dir(0), identity=config.identity()
        )
        assert stored.disposition is AttemptDisposition.ABSENT
        assert "does not decode" in stored.reason

        solver = FakeSolver()
        runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        outcomes = await runner.run_attempts(
            [make_problem("s1")], config, output_dir=store.round_dir(0)
        )
        assert runner.attempt_failures == ()
        assert solver.calls != []  # driven fresh, not lost
        assert outcomes[0].attempt.attempt_id != "preserved-id"

    async def test_a_request_naming_another_attempt_drives_the_problem_again(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        tmp_path: Path,
    ) -> None:
        config = make_config()
        request_path = await self._escalated_checkpoint(store, config, tmp_path)
        # A complete request — for a different attempt.
        first = make_runner(
            solver=FakeSolver([SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH)]),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=ScriptedEscalationChannel([EscalationVerdict.CONTINUE]),
        )
        await first.run_attempts(
            [make_problem("other")], make_config(run_id="other-run"), output_dir=store.round_dir(1)
        )
        foreign = next((store.round_dir(1) / "escalations").glob("esc-*.json"))
        payload = json.loads(foreign.read_text())
        payload["request"]["request_id"] = "E1"
        request_path.write_text(json.dumps(payload), encoding="utf-8")

        stored = await store.load_stored_attempt(
            "s1", output_dir=store.round_dir(0), identity=config.identity()
        )
        assert stored.disposition is AttemptDisposition.ABSENT
        assert "preserved-id" in stored.reason

        solver = FakeSolver()
        runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempts([make_problem("s1")], config, output_dir=store.round_dir(0))
        assert runner.attempt_failures == ()
        assert solver.calls != []


class TestTheCompleteGateChecksTheAttemptId:
    """Finding 7: a previous generation's finished record does not bless a checkpoint."""

    async def test_a_summary_naming_another_attempt_does_not_complete_the_checkpoint(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        problem = make_problem("s1", scores=(1.0,))
        config = make_config()
        first = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await first.run_attempts([problem], config, output_dir=store.round_dir(0))
        checkpoint = store.round_dir(0) / "checkpoints" / "s1.json"
        gen1 = json.loads(checkpoint.read_text())
        # A second generation that died before it could report, hand-written
        # over the same problem's checkpoint: terminal, same identity, and a
        # score nobody's summary describes.
        gen2 = dict(gen1)
        gen2["attempt_id"] = "generation-two"
        gen2["result"] = {**gen1["result"], "score": 42.0}
        checkpoint.write_text(json.dumps(gen2))

        stored = await store.load_stored_attempt(
            "s1", output_dir=store.round_dir(0), identity=config.identity()
        )
        assert stored.disposition is AttemptDisposition.ABSENT
        assert "generation-two" in stored.reason

        solver = FakeSolver()
        second = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        outcomes = await second.run_attempts([problem], config, output_dir=store.round_dir(0))
        assert second.skipped_problems == ()
        assert solver.calls != []
        assert outcomes[0].best_result is not None
        assert outcomes[0].best_result.score == 1.0  # measured, not the 42.0 off disk

    async def test_a_log_naming_another_attempt_does_not_complete_the_checkpoint(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        problem = make_problem("s1")
        config = make_config()
        first = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await first.run_attempts([problem], config, output_dir=store.round_dir(0))
        log_path = store.round_dir(0) / "attempts" / "s1.json"
        log = json.loads(log_path.read_text())
        log["attempt_id"] = "generation-zero"
        log_path.write_text(json.dumps(log))

        stored = await store.load_stored_attempt(
            "s1", output_dir=store.round_dir(0), identity=config.identity()
        )
        assert stored.disposition is AttemptDisposition.ABSENT
        assert "no readable attempt log" in stored.reason

    async def test_a_terminal_checkpoint_with_no_attempt_log_is_not_complete(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """The attempt-log half of the gate, in isolation (metrics.json stays)."""
        problem = make_problem("s1")
        config = make_config()
        first = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await first.run_attempts([problem], config, output_dir=store.round_dir(0))
        (store.round_dir(0) / "attempts" / "s1.json").unlink()
        assert (store.round_dir(0) / "attempts" / "s1" / "metrics.json").exists()

        stored = await store.load_stored_attempt(
            "s1", output_dir=store.round_dir(0), identity=config.identity()
        )
        assert stored.disposition is AttemptDisposition.ABSENT
        solver = FakeSolver()
        second = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        await second.run_attempts([problem], config, output_dir=store.round_dir(0))
        assert solver.calls != []
        assert second.skipped_problems == ()


class TestAReopenedEscalationDoesNotRepageOnEveryRestart:
    """Finding 8: the restart polls for the answer first and keeps the cadence."""

    async def test_the_runner_marks_the_reopened_wait(
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
        request_id = dying.requests[0].request_id

        channel = ScriptedEscalationChannel([EscalationVerdict.ABANDON])
        second = make_runner(
            solver=FakeSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        await second.run_attempts([problem], config, output_dir=store.round_dir(0))
        assert channel.calls == [(request_id, True)]

    async def test_an_answer_dropped_while_down_is_consumed_without_a_page(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        config = make_config()
        problem = make_problem("s1")
        out = store.round_dir(0)
        escalations_dir = out / "escalations"
        dying = DyingChannel()
        first = make_runner(
            solver=FakeSolver([SolverStep(tokens=1, escalate=EscalationReason.NO_VIABLE_APPROACH)]),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=dying,
        )
        with pytest.raises(KeyboardInterrupt):
            await first.run_attempts([problem], config, output_dir=out)
        request_id = dying.requests[0].request_id
        # The operator answers while the process is down.
        (escalations_dir / f"{request_id}.decision.json").write_text(
            json.dumps({"request_id": request_id, "verdict": "abandon", "decided_at_ms": 1}),
            encoding="utf-8",
        )

        ntfy = RecordingNtfyClient()
        channel = OperatorEscalationChannel(
            FileDropDecisionInbox(escalations_dir, clock=clock),
            NtfyEscalationNotifier(ntfy),
            poll_interval_seconds=0.001,
            repush_interval_seconds=None,
            sleep=lambda _s: asyncio.sleep(0),
            clock=clock,
        )
        second = make_runner(
            solver=FakeSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcomes = await second.run_attempts([problem], config, output_dir=out)
        assert outcomes[0].attempt.state is AttemptState.ABANDONED
        assert outcomes[0].escalation_count == 1
        assert ntfy.pushes == []  # the question was already asked; only the answer was read

    async def test_repeated_restarts_do_not_page_before_the_reminder_interval(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        """Channel-level: reopened waits poll first, then keep the cadence."""
        from turing.research.contracts import EscalationRequest

        inbox_dir = tmp_path / "inbox"
        ntfy = RecordingNtfyClient()
        sleeps: list[float] = []
        request = EscalationRequest(
            request_id="esc-reopen",
            problem_id="s1",
            attempt_id="a1",
            round_id="r00",
            reason=EscalationReason.NO_VIABLE_APPROACH,
            summary="stuck",
            cap=DEFAULT_CAP,
            consumed=CapConsumption(),
            created_at_ms=1,
        )
        inbox = FileDropDecisionInbox(inbox_dir, clock=clock)

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            # The operator answers after the third poll — inside the reminder
            # interval on the first restart, past it on the second.
            if len(sleeps) == 3:
                inbox_dir.mkdir(parents=True, exist_ok=True)
                inbox.decision_path("esc-reopen").write_text(
                    json.dumps(
                        {"request_id": "esc-reopen", "verdict": "abandon", "decided_at_ms": 1}
                    ),
                    encoding="utf-8",
                )

        channel = OperatorEscalationChannel(
            inbox,
            NtfyEscalationNotifier(ntfy),
            poll_interval_seconds=1.0,
            repush_interval_seconds=10.0,
            sleep=sleep,
            clock=clock,
        )
        decision = await channel.request_decision(request, reopened=True)
        assert decision.verdict is EscalationVerdict.ABANDON
        assert ntfy.pushes == []  # three polls, no page: within the reminder interval

        # A fresh (not reopened) request pages immediately — the control.
        sleeps.clear()
        fresh = OperatorEscalationChannel(
            inbox,
            NtfyEscalationNotifier(ntfy),
            poll_interval_seconds=1.0,
            repush_interval_seconds=10.0,
            sleep=sleep,
            clock=clock,
        )
        await fresh.request_decision(replace(request, request_id="esc-reopen"))
        assert len(ntfy.pushes) == 1

    async def test_a_reopened_wait_that_outlasts_the_interval_reminds_once(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        from turing.research.contracts import EscalationRequest

        inbox_dir = tmp_path / "inbox"
        ntfy = RecordingNtfyClient()
        polls = 0
        request = EscalationRequest(
            request_id="esc-late",
            problem_id="s1",
            attempt_id="a1",
            round_id="r00",
            reason=EscalationReason.NO_VIABLE_APPROACH,
            summary="stuck",
            cap=DEFAULT_CAP,
            consumed=CapConsumption(),
            created_at_ms=1,
        )
        inbox = FileDropDecisionInbox(inbox_dir, clock=clock)

        async def sleep(_seconds: float) -> None:
            nonlocal polls
            polls += 1
            if polls == 4:  # 4 s waited > 3 s interval: one reminder has gone out
                inbox_dir.mkdir(parents=True, exist_ok=True)
                inbox.decision_path("esc-late").write_text(
                    json.dumps(
                        {"request_id": "esc-late", "verdict": "continue", "decided_at_ms": 1}
                    ),
                    encoding="utf-8",
                )

        channel = OperatorEscalationChannel(
            inbox,
            NtfyEscalationNotifier(ntfy),
            poll_interval_seconds=1.0,
            repush_interval_seconds=3.0,
            sleep=sleep,
            clock=clock,
        )
        await channel.request_decision(request, reopened=True)
        assert len(ntfy.pushes) == 1


class TestAnUnreadableCheckpointIsAbsentNotFatal:
    """Finding 9: nothing the resume probe reads may take the round down."""

    async def test_a_non_utf8_checkpoint_drives_the_problem_and_the_rest_of_the_round(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        config = make_config()
        corpus = [make_problem("s1"), make_problem("s2")]
        path = store.attempt_checkpoint_path("s1", output_dir=store.round_dir(0))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"attempt_id": "\xff\xfe\x00bad"}')

        stored = await store.load_stored_attempt(
            "s1", output_dir=store.round_dir(0), identity=config.identity()
        )
        assert stored.disposition is AttemptDisposition.ABSENT
        assert "could not be read" in stored.reason

        solver = FakeSolver()
        runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        outcomes = await runner.run_attempts(corpus, config, output_dir=store.round_dir(0))
        assert [o.problem.id for o in outcomes] == ["s1", "s2"]
        assert runner.attempt_failures == ()
        assert {problem_id for problem_id, _ in solver.calls} == {"s1", "s2"}

    async def test_a_directory_where_a_checkpoint_should_be_is_absent_and_contained(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """The probe says ABSENT; the write that then cannot land is contained.

        A directory squatting on a checkpoint path is a corrupted results
        tree, and this process cannot write a checkpoint there. That is a
        real per-problem I/O failure — reported as a lost attempt — and not a
        round-killing one: the second problem still measures.
        """
        config = make_config()
        path = store.attempt_checkpoint_path("s1", output_dir=store.round_dir(0))
        path.mkdir(parents=True, exist_ok=True)
        stored = await store.load_stored_attempt(
            "s1", output_dir=store.round_dir(0), identity=config.identity()
        )
        assert stored.disposition is AttemptDisposition.ABSENT

        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcomes = await runner.run_attempts(
            [make_problem("s1"), make_problem("s2")], config, output_dir=store.round_dir(0)
        )
        assert [o.problem.id for o in outcomes] == ["s2"]
        assert [f.problem_id for f in runner.attempt_failures] == ["s1"]
        assert "IsADirectoryError" in runner.attempt_failures[0].error

    async def test_a_probe_that_raises_costs_the_problem_not_the_round(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """The containment guards 'it raised', not any listed cause."""
        config = make_config()
        corpus = [make_problem("s1"), make_problem("s2")]
        original = store.load_stored_attempt

        async def exploding(problem_id: str, **kwargs: object) -> StoredAttempt:
            if problem_id == "s1":
                raise RuntimeError("results tree on a flaky mount")
            return await original(problem_id, **kwargs)  # type: ignore[arg-type]

        store.load_stored_attempt = exploding  # type: ignore[method-assign]
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcomes = await runner.run_attempts(corpus, config, output_dir=store.round_dir(0))
        assert [o.problem.id for o in outcomes] == ["s2"]
        assert [f.problem_id for f in runner.attempt_failures] == ["s1"]


class TestRunIdentityIsolatesEachCoordinate:
    """Finding 12: each identity field alone forces a re-drive."""

    @pytest.mark.parametrize(
        "mutation",
        [
            pytest.param({"seed": 99}, id="seed"),
            pytest.param({"run_id": "other"}, id="round_id"),
        ],
    )
    async def test_one_changed_coordinate_re_drives(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        mutation: dict,  # type: ignore[type-arg]
    ) -> None:
        problem = make_problem("s1")
        config = make_config()
        first = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await first.run_attempts([problem], config, output_dir=store.round_dir(0))

        solver = FakeSolver()
        second = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        await second.run_attempts(
            [problem], replace(config, **mutation), output_dir=store.round_dir(0)
        )
        assert solver.calls != [], f"{mutation} was ignored by RunIdentity"
        assert second.skipped_problems == ()

    async def test_only_the_eval_set_hash_differing_re_drives(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        problem = make_problem("s1")
        config = make_config(eval_set_hash="a" * 64)
        first = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await first.run_attempts([problem], config, output_dir=store.round_dir(0))
        assert (
            json.loads((store.round_dir(0) / "checkpoints" / "s1.json").read_text())[
                "eval_set_hash"
            ]
            == "a" * 64
        )

        solver = FakeSolver()
        second = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        await second.run_attempts(
            [problem], replace(config, eval_set_hash="b" * 64), output_dir=store.round_dir(0)
        )
        assert solver.calls != []
        assert second.skipped_problems == ()

    async def test_the_stored_identity_carries_all_four_coordinates(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        config = make_config(eval_set_hash="a" * 64)
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempts([make_problem("s1")], config, output_dir=store.round_dir(0))
        raw = json.loads((store.round_dir(0) / "checkpoints" / "s1.json").read_text())
        assert raw["round_id"] == config.run_id
        assert raw["seed"] == config.seed
        assert raw["eval_set_hash"] == "a" * 64
        assert raw["config_digest"] == config.config_digest()
        assert (
            RunIdentity(
                round_id=raw["round_id"],
                seed=raw["seed"],
                eval_set_hash=raw["eval_set_hash"],
                config_digest=raw["config_digest"],
            )
            == config.identity()
        )
