"""The noise-floor run: >=3 seeds, its own artifact, never a trajectory row."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import ContractViolationError, ProblemType, Split
from turing.research.loop.metrics import NoiseFloorStatistic
from turing.research.loop.noise_floor import NoiseFloorConfig, NoiseFloorRunner
from turing.research.loop.protocols import SolverStep

from .conftest import DEFAULT_CAP, ENGINE, FakeSolver, make_config, make_problem, make_runner

if TYPE_CHECKING:
    from turing.research.loop.trajectory import TrajectoryStore

    from .conftest import FakeClock, TempWorkspaceProvider


def make_nf_config(*, seeds: tuple[int, ...] = (1, 2, 3), **kwargs) -> NoiseFloorConfig:
    return NoiseFloorConfig(
        run_id="nf",
        eval_set_hash="",
        engine=ENGINE,
        seeds=seeds,
        default_cap=DEFAULT_CAP,
        **kwargs,
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
