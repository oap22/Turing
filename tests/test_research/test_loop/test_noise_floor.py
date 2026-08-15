"""The noise-floor run: >=3 seeds, its own artifact, never a trajectory row."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest
import structlog.testing

from turing.research.contracts import ContractViolationError, ProblemType, Split
from turing.research.loop.metrics import NoiseFloorStatistic
from turing.research.loop.noise_floor import NoiseFloorConfig, NoiseFloorRunner
from turing.research.loop.protocols import SolverStep

from .conftest import (
    DEFAULT_CAP,
    ENGINE,
    FakeSolver,
    TempWorkspaceProvider,
    make_config,
    make_problem,
    make_runner,
)

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import Problem
    from turing.research.loop.trajectory import TrajectoryStore

    from .conftest import FakeClock


def make_nf_config(*, seeds: tuple[int, ...] = (1, 2, 3), **kwargs) -> NoiseFloorConfig:
    return NoiseFloorConfig(
        run_id="nf",
        eval_set_hash="",
        engine=ENGINE,
        seeds=seeds,
        default_cap=DEFAULT_CAP,
        **kwargs,
    )


class OneSeedFlakyWorkspaces(TempWorkspaceProvider):
    """Fails ``materialise`` for one problem on one seed only.

    Stands in for the *transient* loss — I/O, a killed process, a template
    that vanished mid-run — which is the case that silently corrupts a floor,
    because the other seeds keep the problem and only this one drops it. It
    fails at workspace materialisation because that is the earliest thing
    :meth:`RoundRunner.run_attempt` does and the only failure mode a test can
    aim at a single seed: the solver and the verifier are both shared objects
    with no seed of their own, and both of *their* exceptions are caught
    inside ``run_attempt`` and escalated rather than raised out of it.

    The attempt id is ``<run_id>-<problem_id>-<hex>`` and the noise floor's
    per-seed run id is ``<run_id>-seed-<n>``, so matching on the prefix is
    enough to pick out exactly one (seed, problem) pair.
    """

    def __init__(self, root: Path, *, run_id: str, problem_id: str) -> None:
        super().__init__(root)
        self._prefix = f"{run_id}-{problem_id}-"
        self.refusals = 0

    async def materialise(self, problem: Problem, *, attempt_id: str) -> Path:
        if attempt_id.startswith(self._prefix):
            self.refusals += 1
            raise OSError("workspace template unreadable")
        return await super().materialise(problem, attempt_id=attempt_id)


def problem_with_scale(problem_id: str, scale: str) -> Problem:
    """A problem whose verifier reports a colliding ``score_scale``.

    Stands in for the *deterministic* loss. ``score_scale`` is free-form by
    design and names the metrics key holding the raw score, so a scale
    colliding with a reserved key is refused by
    ``results._validate_scored_metric`` at that problem's first verification —
    identically on every seed, because it is a property of the problem.
    """
    problem = make_problem(problem_id)
    return dataclasses.replace(
        problem, verifier=dataclasses.replace(problem.verifier, score_scale=scale)
    )


class SeedSensitiveSolver(FakeSolver):
    """A solver whose reported tokens vary by seed — stands in for stochasticity."""

    async def step(self, task, attempt):  # type: ignore[no-untyped-def]
        self.calls.append((task.id, attempt.step_index))
        return SolverStep(tokens=attempt.seed, note=f"seed {attempt.seed}")


class TestConfig:
    def test_fewer_than_three_seeds_is_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="at least 3 seeds"):
            make_nf_config(seeds=(1, 2))

    def test_duplicate_seeds_are_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="distinct"):
            make_nf_config(seeds=(1, 1, 2))

    def test_each_seed_gets_the_same_config_but_its_own_run_id(self) -> None:
        config = make_nf_config()
        first = config.round_config_for(1)
        second = config.round_config_for(2)
        assert first.run_id != second.run_id
        assert first.eval_set_hash == second.eval_set_hash
        assert first.default_cap == second.default_cap
        assert first.parent_round_id is None


class TestRun:
    def corpus(self) -> list:
        return [
            make_problem("speed-1", scores=(2.0,)),
            make_problem("kaggle-1", scores=(0.4,), problem_type=ProblemType.KAGGLE),
        ]

    async def test_it_runs_every_seed_and_writes_its_own_artifact(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(
            solver=SeedSensitiveSolver(), store=store, workspaces=workspaces, clock=clock
        )
        report = await NoiseFloorRunner(runner, store).run(self.corpus(), make_nf_config())

        assert report.seeds == (1, 2, 3)
        assert store.noise_floor_path.exists()
        assert store.noise_floor_path.parent.name == "noise-floor"
        for seed in (1, 2, 3):
            assert (store.noise_floor_seed_dir(seed) / "attempts" / "speed-1.json").exists()

        body = json.loads(store.noise_floor_path.read_text())
        assert set(body["floors"]) == {"speedup/practice", "kaggle/practice"}
        assert set(body["per_seed"]) == {"1", "2", "3"}
        assert body["statistic"] == "stdev"

    async def test_seed_runs_never_enter_the_trajectory(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """A seed run has no parent and is not a point on the curve."""
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await NoiseFloorRunner(runner, store).run(self.corpus(), make_nf_config())
        document = await store.load_trajectory()
        assert document["rounds"] == []

    async def test_the_floor_feeds_straight_into_a_round(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        report = await NoiseFloorRunner(runner, store).run(self.corpus(), make_nf_config())
        base = await runner.run_round(self.corpus(), make_config(), noise_floor=report)
        assert base.record.gates["noise_floor_available"] is True
        assert base.trajectory_row is not None
        assert set(base.trajectory_row["noise_floor"]) == {
            "speedup/practice",
            "kaggle/practice",
        }

    async def test_a_deterministic_solver_yields_a_degenerate_floor(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Three identical seeds resolve no noise — and that must be visible."""
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        report = await NoiseFloorRunner(runner, store).run(self.corpus(), make_nf_config())
        floor = report.floor_for((ProblemType.SPEEDUP, Split.PRACTICE))
        assert floor is not None
        assert floor.is_degenerate is True
        body = json.loads(store.noise_floor_path.read_text())
        assert body["floors"]["speedup/practice"]["degenerate"] is True

    async def test_the_range_statistic_is_recorded(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        report = await NoiseFloorRunner(runner, store).run(
            self.corpus(), make_nf_config(statistic=NoiseFloorStatistic.RANGE)
        )
        assert report.statistic is NoiseFloorStatistic.RANGE
        body = json.loads(store.noise_floor_path.read_text())
        assert body["statistic"] == "range"

    async def test_per_seed_scores_are_kept_not_just_the_spread(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Within-project and across-seed spread are two sources; keep both."""
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        report = await NoiseFloorRunner(runner, store).run(self.corpus(), make_nf_config())
        assert set(report.per_seed_scores) == {1, 2, 3}
        body = json.loads(store.noise_floor_path.read_text())
        assert body["per_seed"]["1"][0]["scores"]


class TestASeedThatLostAnAttemptIsRefused:
    """A round survives a lost problem; a noise floor must not.

    ``run_attempts`` contains a raising attempt — the round drops that problem,
    keeps the rest, and flags ``all_attempts_completed=False``. Correct for a
    round, which is one point with an honestly smaller n. Wrong for the floor,
    which is the *divisor* that later rounds' marginal gains are called signal
    or noise against: reduce it from seeds that measured different problem
    sets and the spread is part run-to-run variance and part "which problems
    ran", with nothing downstream able to separate them again.
    ``measure_noise_floor`` cannot catch it — it guards cell *presence*, and a
    cell that merely averaged one fewer problem is still present.

    These tests drive a real, unmocked ``RoundRunner`` and ``NoiseFloorRunner``
    against real failures (an unreadable workspace, a colliding score scale)
    rather than stubbing ``attempt_failures``, so they fail if containment,
    the property or the refusal drift apart.
    """

    def corpus(self) -> list:
        return [
            make_problem("speed-1", scores=(2.0,)),
            make_problem("speed-2", scores=(3.0,)),
        ]

    async def test_a_transient_loss_on_one_seed_names_the_seed_and_the_problem(
        self, store: TrajectoryStore, tmp_path: Path, clock: FakeClock
    ) -> None:
        """The message is the artifact an overnight operator reads.

        Seed 2 loses ``speed-2`` while seeds 1 and 3 keep it — the shifting
        problem set the refusal exists for. The seed number and the problem id
        have to be in the message itself: the alternative is an operator
        reconstructing which of three identical-looking seed directories is
        short a problem from a traceback.
        """
        workspaces = OneSeedFlakyWorkspaces(
            tmp_path / "workspaces", run_id="nf-seed-2", problem_id="speed-2"
        )
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)

        with (
            structlog.testing.capture_logs() as cap,
            pytest.raises(ContractViolationError) as caught,
        ):
            await NoiseFloorRunner(runner, store).run(self.corpus(), make_nf_config())

        message = str(caught.value)
        assert "seed 2" in message
        assert "speed-2" in message
        assert "OSError" in message
        # Why, not just that: the refusal has to survive being quoted in an
        # issue by someone who has never read this module.
        assert "same problem set" in message
        assert workspaces.refusals == 1

        incomplete = [e for e in cap if e.get("event") == "research.noise_floor.seed_incomplete"]
        assert len(incomplete) == 1
        assert incomplete[0]["log_level"] == "error"
        assert incomplete[0]["seed"] == 2
        assert incomplete[0]["lost"] == ["speed-2"]
        assert incomplete[0]["measured"] == 1

    async def test_no_floor_is_written_and_the_later_seeds_are_not_run(
        self, store: TrajectoryStore, tmp_path: Path, clock: FakeClock
    ) -> None:
        """Fail fast, and leave no artifact behind.

        Seed 3 is hours of opportunistic subscription time that could only
        produce a report already known to be unusable. And ``noise-floor.json``
        must stay *absent* rather than partial: absence is what the downstream
        refusals (``compute_deltas``, ``assess_saturation``,
        ``noise_floor_available``) are already built to handle, whereas a file
        on disk is read as a measurement.
        """
        workspaces = OneSeedFlakyWorkspaces(
            tmp_path / "workspaces", run_id="nf-seed-2", problem_id="speed-1"
        )
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)

        with pytest.raises(ContractViolationError):
            await NoiseFloorRunner(runner, store).run(self.corpus(), make_nf_config())

        assert not store.noise_floor_path.exists()
        assert store.noise_floor_seed_dir(1).exists()
        assert not store.noise_floor_seed_dir(3).exists()
        # Nothing leaked into the curve either: a seed run is not a round.
        assert (await store.load_trajectory())["rounds"] == []

    async def test_a_deterministic_loss_is_refused_too_not_just_a_flaky_one(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The tempting exemption, closed deliberately.

        A colliding ``score_scale`` is a property of the problem, so every seed
        loses it identically and the floor is at least internally consistent —
        just measured over a narrower corpus than advertised. It is still
        refused. The runner cannot tell deterministic from transient at the
        point of failure (that is only knowable after the other seeds have run
        and lost the same problem, which is the compute the fail-fast exists to
        save), and a floor measured over one problem is not the yardstick for
        rounds measured over two. Dropping a broken problem belongs in the
        corpus, where it moves ``eval_set_hash``.
        """
        corpus = [make_problem("speed-1", scores=(2.0,)), problem_with_scale("bad", "progress")]
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)

        with pytest.raises(ContractViolationError, match="seed 1") as caught:
            await NoiseFloorRunner(runner, store).run(corpus, make_nf_config())

        assert "bad" in str(caught.value)
        assert not store.noise_floor_path.exists()

    async def test_a_seed_that_loses_every_attempt_still_names_the_seed(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """``run_attempts`` refuses a total loss on its own — but blind to the seed.

        Its message ("every one of the N attempt(s) failed") is correct and
        says nothing about *which* of three seed runs produced it. Same fault
        as a partial loss from where the operator sits, so it gets the same
        message; ``attempt_failures`` is populated before that raise precisely
        so this caller can still list the problems.
        """
        corpus = [problem_with_scale("bad-1", "progress"), problem_with_scale("bad-2", "ts")]
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)

        with pytest.raises(ContractViolationError) as caught:
            await NoiseFloorRunner(runner, store).run(corpus, make_nf_config())

        message = str(caught.value)
        assert "seed 1" in message
        assert "bad-1" in message
        assert "bad-2" in message
        assert isinstance(caught.value.__cause__, ContractViolationError)

    async def test_a_clean_multi_seed_floor_is_unaffected(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The negative half: the guard must not fire on a healthy run.

        A refusal that also triggers on clean input is not a guard, it is an
        outage — and this one is placed on the single step the whole protocol
        depends on, so it gets the same seed-sensitive solver the happy-path
        tests use rather than a deterministic one.
        """
        runner = make_runner(
            solver=SeedSensitiveSolver(), store=store, workspaces=workspaces, clock=clock
        )
        with structlog.testing.capture_logs() as cap:
            report = await NoiseFloorRunner(runner, store).run(self.corpus(), make_nf_config())

        assert report.seeds == (1, 2, 3)
        assert set(report.per_seed_scores) == {1, 2, 3}
        assert store.noise_floor_path.exists()
        assert runner.attempt_failures == ()
        assert not [e for e in cap if e.get("event") == "research.noise_floor.seed_incomplete"]
        # Every seed measured both problems -- the property the refusal guards.
        for scores in report.per_seed_scores.values():
            assert sum(len(cell.scores) for cell in scores) == 2
