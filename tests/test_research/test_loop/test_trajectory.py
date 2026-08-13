"""``trajectory.json``: the four numbers, the layout, and mandatory lineage."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import (
    SCORE_SCALE_SPEEDUP,
    Cap,
    ContractViolationError,
    EngineIdentity,
    ProblemType,
    RoundCost,
    RoundDelta,
    RoundRecord,
    Split,
)
from turing.research.loop.metrics import (
    CostBasis,
    ScoredProblem,
    assess_saturation,
    build_type_scores,
    floors_by_cell,
    measure_noise_floor,
)
from turing.research.loop.trajectory import (
    TRAJECTORY_SCHEMA_VERSION,
    cell_key,
    encode_trajectory_row,
)

from .conftest import ENGINE
from .test_metrics import scored

if TYPE_CHECKING:
    from turing.research.loop.trajectory import TrajectoryStore

COST = RoundCost(wall_clock_seconds=120.0, tokens=4000, attempts=3)


def make_record(
    *,
    round_index: int = 0,
    run_id: str = "r00",
    parent_round_id: str | None = None,
    eval_set_hash: str = "corpus-v1",
    speedup: float = 2.0,
    kaggle: float = 0.4,
    deltas: tuple[RoundDelta, ...] = (),
    engine: EngineIdentity = ENGINE,
    escalations: int = 0,
) -> RoundRecord:
    return RoundRecord(
        round_index=round_index,
        run_id=run_id,
        parent_round_id=parent_round_id,
        eval_set_hash=eval_set_hash,
        engine=engine,
        type_scores=build_type_scores(
            [
                scored("s1", speedup),
                scored("k1", kaggle, problem_type=ProblemType.KAGGLE),
            ]
        ),
        deltas=deltas,
        cost=COST,
        escalation_count=escalations,
        created_at_ms=1,
        gates={"eval_set_stable": True},
        verdict="baseline",
    )


class TestRowShape:
    def test_primary_is_per_cell_not_a_scalar(self) -> None:
        row = encode_trajectory_row(make_record())
        assert isinstance(row["primary"], dict)
        assert row["primary"] == {
            "kaggle/practice": pytest.approx(0.4),
            "speedup/practice": pytest.approx(2.0),
        }

    def test_row_carries_all_four_driving_function_slots(self) -> None:
        row = encode_trajectory_row(make_record(escalations=2))
        for key in ("primary", "delta", "noise_floor", "cost", "human_interventions"):
            assert key in row
        assert row["human_interventions"] == 2

    def test_cost_has_no_dollar_field(self) -> None:
        """Metering was retired; a dollars field would resurrect BudgetGate."""
        row = encode_trajectory_row(make_record())
        assert set(row["cost"]) == {"wall_clock_seconds", "tokens", "attempts"}

    def test_lineage_fields_are_present(self) -> None:
        row = encode_trajectory_row(make_record(round_index=1, run_id="r01", parent_round_id="r00"))
        assert row["round"] == 1
        assert row["parent_round"] == "r00"
        assert row["eval_set_hash"] == "corpus-v1"
        assert row["engine"]["scaffold_git_sha"] == "abc1234"

    def test_cell_key_never_pools_types(self) -> None:
        assert cell_key((ProblemType.SPEEDUP, Split.HELD_OUT)) == "speedup/held_out"

    def test_n_is_per_cell_not_a_scalar(self) -> None:
        row = encode_trajectory_row(make_record())
        assert row["n"] == {"kaggle/practice": 1, "speedup/practice": 1}

    def test_a_floored_cell_mean_is_not_byte_identical_to_a_measured_one(self) -> None:
        """Finding 10: a 1.0 built from abandoned floors must not look measured."""
        measured = [scored("s1", 1.0), scored("s2", 1.0)]
        floored = [
            ScoredProblem(
                problem_id="s1",
                problem_type=ProblemType.SPEEDUP,
                split=Split.PRACTICE,
                score=1.0,
                passed_correctness=False,
                score_scale=SCORE_SCALE_SPEEDUP,
                scored=False,
            ),
            ScoredProblem(
                problem_id="s2",
                problem_type=ProblemType.SPEEDUP,
                split=Split.PRACTICE,
                score=1.0,
                passed_correctness=False,
                score_scale=SCORE_SCALE_SPEEDUP,
                scored=False,
            ),
        ]
        measured_record = RoundRecord(
            round_index=0,
            run_id="r00",
            parent_round_id=None,
            eval_set_hash="corpus-v1",
            engine=ENGINE,
            type_scores=build_type_scores(measured),
            deltas=(),
            cost=COST,
            escalation_count=0,
            created_at_ms=1,
            gates={"eval_set_stable": True},
            verdict="baseline",
        )
        floored_record = RoundRecord(
            round_index=0,
            run_id="r00",
            parent_round_id=None,
            eval_set_hash="corpus-v1",
            engine=ENGINE,
            type_scores=build_type_scores(floored),
            deltas=(),
            cost=COST,
            escalation_count=0,
            created_at_ms=1,
            gates={"eval_set_stable": True},
            verdict="baseline",
        )
        measured_row = encode_trajectory_row(measured_record, scored=measured)
        floored_row = encode_trajectory_row(floored_record, scored=floored)
        assert measured_row["primary"] == floored_row["primary"]
        assert measured_row["n"] == floored_row["n"] == {"speedup/practice": 2}
        assert measured_row["floored_n"]["speedup/practice"] == 0
        assert floored_row["floored_n"]["speedup/practice"] == 2
        assert measured_row["scored_n"]["speedup/practice"] == 2
        assert floored_row["scored_n"]["speedup/practice"] == 0

    def test_row_records_seed_cap_and_verify_every_step(self) -> None:
        cap = Cap(max_steps=3, max_tokens=10_000, max_wall_clock_seconds=600.0)
        row = encode_trajectory_row(
            make_record(),
            seed=7,
            cap=cap,
            verify_every_step=False,
        )
        assert row["seed"] == 7
        assert row["cap"] == {
            "max_steps": 3,
            "max_tokens": 10_000,
            "max_wall_clock_seconds": 600.0,
        }
        assert row["verify_every_step"] is False

    def test_saturation_refusals_are_written_verbatim(self) -> None:
        current = build_type_scores([scored("s1", 1.5)])
        parent = build_type_scores([scored("s1", 1.0)])
        row = encode_trajectory_row(
            make_record(), assessments=assess_saturation(current, parent, {})
        )
        assert row["saturation"][0]["verdict"] == "refused_no_noise_floor"


class TestAppend:
    async def test_round_zero_writes_a_document(self, store: TrajectoryStore) -> None:
        await store.ensure_layout()
        await store.append_round(make_record(), cost_basis=CostBasis.WALL_CLOCK)
        body = json.loads(store.trajectory_path.read_text())
        assert body["schema_version"] == TRAJECTORY_SCHEMA_VERSION
        assert body["loop_slug"] == "test-loop"
        assert len(body["rounds"]) == 1
        assert body["rounds"][0]["cost_basis"] == "wall_clock_seconds"

    async def test_audit_fields_survive_onto_disk(self, store: TrajectoryStore) -> None:
        cap = Cap(max_steps=3, max_tokens=10_000, max_wall_clock_seconds=600.0)
        items = [scored("s1", 1.0), scored("k1", 0.4, problem_type=ProblemType.KAGGLE)]
        row = await store.append_round(
            make_record(),
            scored=items,
            seed=11,
            cap=cap,
            verify_every_step=False,
        )
        body = json.loads(store.trajectory_path.read_text())
        persisted = body["rounds"][0]
        assert persisted["n"] == row["n"] == {"kaggle/practice": 1, "speedup/practice": 1}
        assert persisted["scored_n"] == {"kaggle/practice": 1, "speedup/practice": 1}
        assert persisted["floored_n"] == {"kaggle/practice": 0, "speedup/practice": 0}
        assert persisted["seed"] == 11
        assert persisted["cap"]["max_steps"] == 3
        assert persisted["verify_every_step"] is False

    async def test_second_round_on_the_same_eval_set_is_comparable(
        self, store: TrajectoryStore
    ) -> None:
        await store.append_round(make_record())
        row = await store.append_round(
            make_record(round_index=1, run_id="r01", parent_round_id="r00")
        )
        assert row["comparable_to_parent"] is True
        assert row["trajectory_restart"] is False
        assert row["engine_changed"] is False

    async def test_a_changed_eval_set_is_flagged_incomparable(self, store: TrajectoryStore) -> None:
        await store.append_round(make_record())
        row = await store.append_round(
            make_record(
                round_index=1,
                run_id="r01",
                parent_round_id="r00",
                eval_set_hash="corpus-v2",
            )
        )
        assert row["comparable_to_parent"] is False
        assert row["trajectory_restart"] is True

    async def test_a_changed_engine_is_flagged_as_a_confound(self, store: TrajectoryStore) -> None:
        await store.append_round(make_record())
        row = await store.append_round(
            make_record(
                round_index=1,
                run_id="r01",
                parent_round_id="r00",
                engine=EngineIdentity(
                    backend="claude",
                    orchestrator_model="opus",
                    substep_model="haiku",
                    scaffold_git_sha="def5678",
                ),
            )
        )
        assert row["engine_changed"] is True

    async def test_the_trajectory_is_append_only(self, store: TrajectoryStore) -> None:
        await store.append_round(make_record())
        with pytest.raises(ContractViolationError, match="append-only"):
            await store.append_round(make_record())

    async def test_noise_floor_presence_is_recorded_on_every_row(
        self, store: TrajectoryStore
    ) -> None:
        row = await store.append_round(make_record())
        assert row["noise_floor_measured"] is False

        floors = measure_noise_floor(
            {
                seed: build_type_scores(
                    [
                        scored("s1", 1.0 + 0.1 * seed),
                        scored("k1", 0.4, problem_type=ProblemType.KAGGLE),
                    ]
                )
                for seed in (1, 2, 3)
            }
        )
        row2 = await store.append_round(
            make_record(round_index=1, run_id="r01", parent_round_id="r00"),
            noise_floors=floors,
        )
        assert row2["noise_floor_measured"] is True
        assert set(row2["noise_floor"]) == {"speedup/practice", "kaggle/practice"}

    async def test_delta_and_cost_per_point_are_per_cell(self, store: TrajectoryStore) -> None:
        floors = floors_by_cell(
            measure_noise_floor(
                {
                    seed: build_type_scores(
                        [
                            scored("s1", 1.0 + 0.01 * seed),
                            scored("k1", 0.4, problem_type=ProblemType.KAGGLE),
                        ]
                    )
                    for seed in (1, 2, 3)
                }
            )
        )
        deltas = (
            RoundDelta(
                problem_type=ProblemType.SPEEDUP,
                split=Split.PRACTICE,
                marginal_gain=1.0,
                noise_floor=floors[(ProblemType.SPEEDUP, Split.PRACTICE)].value,
                cost_per_unit_gain=120.0,
            ),
        )
        row = encode_trajectory_row(
            make_record(round_index=1, run_id="r01", parent_round_id="r00", deltas=deltas)
        )
        assert row["delta"] == {"speedup/practice": pytest.approx(1.0)}
        assert row["cost_per_point"] == {"speedup/practice": pytest.approx(120.0)}
        assert row["delta_in_noise_units"]["speedup/practice"] > 1


class TestLayout:
    def test_directory_names_match_driving_functions(self, store: TrajectoryStore) -> None:
        assert store.loop_dir.name == "loop-test-loop"
        assert store.trajectory_path.name == "trajectory.json"
        assert store.noise_floor_dir.name == "noise-floor"
        assert store.round_dir(0).name == "round-00"
        assert store.round_dir(12).name == "round-12"
        assert store.noise_floor_seed_dir(3).name == "seed-3"

    async def test_ensure_layout_creates_the_loop_and_noise_floor_dirs(
        self, store: TrajectoryStore
    ) -> None:
        await store.ensure_layout()
        assert store.loop_dir.is_dir()
        assert store.noise_floor_dir.is_dir()
