"""The loop-2 seam: practice only, never held-out, and unused in loop 1."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import (
    ContractViolationError,
    ProblemType,
    Split,
)
from turing.research.loop import runner as runner_module
from turing.research.loop.metrics import build_type_scores
from turing.research.loop.self_edit_seam import (
    SelfEditInputs,
    collect_self_edit_inputs,
)

from .conftest import FakeSolver, make_config, make_problem, make_runner
from .test_metrics import scored

if TYPE_CHECKING:
    from turing.research.loop.trajectory import TrajectoryStore

    from .conftest import FakeClock, TempWorkspaceProvider


def corpus() -> list:
    return [
        make_problem("speed-practice", scores=(2.0,)),
        make_problem("speed-held", scores=(9.0,), split=Split.HELD_OUT),
        make_problem("kaggle-practice", scores=(0.4,), problem_type=ProblemType.KAGGLE),
    ]


class TestScope:
    def test_loop_one_never_calls_the_seam(self) -> None:
        """The runner must not reach into the self-edit path."""
        source = inspect.getsource(runner_module)
        assert "self_edit_seam" in source  # only as a docstring pointer
        assert "collect_self_edit_inputs" not in source

    def test_the_step_protocol_has_no_implementation(self) -> None:
        from turing.research.loop.self_edit_seam import SelfEditStep

        assert inspect.isclass(SelfEditStep)
        assert getattr(SelfEditStep, "_is_protocol", False) is True


class TestPracticeOnly:
    async def test_held_out_never_enters_the_summary(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round(corpus(), make_config())
        inputs = collect_self_edit_inputs(outcome.record, outcome.attempts)

        assert {ts.split for ts in inputs.practice_scores} == {Split.PRACTICE}
        assert "speed-held" not in inputs.per_problem
        assert set(inputs.per_problem) == {"speed-practice", "kaggle-practice"}
        assert all(item["problem_id"] != "speed-held" for item in inputs.sampled_trajectories)

    async def test_the_round_still_scored_and_reported_the_held_out_problems(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Score everything; learn from a subset only."""
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round(corpus(), make_config())
        assert outcome.record.score_for(ProblemType.SPEEDUP, Split.HELD_OUT) is not None
        assert (store.round_dir(0) / "attempts" / "speed-held.json").exists()

    def test_a_held_out_cell_reaching_the_record_is_a_hard_error(self) -> None:
        with pytest.raises(ContractViolationError, match="held-out cell"):
            SelfEditInputs(
                round_index=1,
                run_id="r01",
                eval_set_hash="corpus-v1",
                scaffold_git_sha="abc",
                practice_scores=build_type_scores([scored("s1", 1.0, split=Split.HELD_OUT)]),
                sampled_trajectories=(),
                per_problem={},
            )

    async def test_the_channel_is_a_handful_of_trajectories_not_all_of_them(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round(corpus(), make_config())
        inputs = collect_self_edit_inputs(outcome.record, outcome.attempts, sample_limit=1)
        assert len(inputs.sampled_trajectories) == 1
        # Lowest-scoring practice attempt first: kaggle 0.4 below speedup 2.0.
        assert inputs.sampled_trajectories[0]["problem_id"] == "kaggle-practice"

    async def test_sampled_trajectories_are_not_a_handle_to_the_attempts_dir(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Finding 12: a Path in attempts/ has held-out logs one glob away."""
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round(corpus(), make_config())
        inputs = collect_self_edit_inputs(outcome.record, outcome.attempts)
        attempts_dir = store.round_dir(0) / "attempts"
        held = attempts_dir / "speed-held.json"
        assert held.exists()
        siblings = {p.name for p in attempts_dir.glob("*.json")}
        assert "speed-held.json" in siblings
        for item in inputs.sampled_trajectories:
            assert not isinstance(item, Path)
            assert not hasattr(item, "parent")
            assert item["problem_id"] != "speed-held"
        with pytest.raises(ContractViolationError, match="not paths"):
            SelfEditInputs(
                round_index=outcome.record.round_index,
                run_id=outcome.record.run_id,
                eval_set_hash=outcome.record.eval_set_hash,
                scaffold_git_sha=outcome.record.engine.scaffold_git_sha,
                practice_scores=outcome.record.self_edit_visible_scores(),
                sampled_trajectories=(held,),  # type: ignore[arg-type]
                per_problem={},
            )

    async def test_a_zero_sample_limit_is_refused(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round(corpus(), make_config())
        with pytest.raises(ContractViolationError, match="no trajectories"):
            collect_self_edit_inputs(outcome.record, outcome.attempts, sample_limit=0)

    async def test_the_error_taxonomy_slot_is_empty_in_loop_one(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """It must exist before round 0 of loop 2 and stay frozen; not built here."""
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round(corpus(), make_config())
        inputs = collect_self_edit_inputs(outcome.record, outcome.attempts)
        assert dict(inputs.error_taxonomy) == {}
