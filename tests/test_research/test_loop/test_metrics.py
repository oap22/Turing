"""The measurement layer: cells, floors, refusals.

The two properties under test here are the ones that make every downstream
number trustworthy: **no noise floor means no saturation verdict**, and
**problem types are never averaged together**.
"""

from __future__ import annotations

import pytest

from turing.research.contracts import (
    HARNESS_FAILURE_KEY,
    SCORE_SCALE_LEADERBOARD_PERCENTILE,
    SCORE_SCALE_SPEEDUP,
    ContractViolationError,
    ProblemType,
    RoundCost,
    Split,
    VerificationResult,
)
from turing.research.loop import metrics as metrics_mod
from turing.research.loop.metrics import (
    MIN_NOISE_FLOOR_SEEDS,
    CostBasis,
    NoiseFloorStatistic,
    SaturationVerdict,
    ScoredProblem,
    assess_saturation,
    build_type_scores,
    compute_deltas,
    cost_per_unit_gain,
    floors_by_cell,
    measure_noise_floor,
    round_verdict,
)

from .conftest import make_problem

COST = RoundCost(wall_clock_seconds=100.0, tokens=1000, attempts=2)


def scored(
    problem_id: str,
    score: float,
    *,
    problem_type: ProblemType = ProblemType.SPEEDUP,
    split: Split = Split.PRACTICE,
    correct: bool = True,
) -> ScoredProblem:
    scale = (
        SCORE_SCALE_SPEEDUP
        if problem_type is ProblemType.SPEEDUP
        else SCORE_SCALE_LEADERBOARD_PERCENTILE
    )
    return ScoredProblem(
        problem_id=problem_id,
        problem_type=problem_type,
        split=split,
        score=score,
        passed_correctness=correct,
        score_scale=scale,
    )


class TestPublicSurface:
    def test_every_exported_name_exists(self) -> None:
        """A star-import of this module used to fail: __all__ named four undefined symbols."""
        for name in metrics_mod.__all__:
            assert hasattr(metrics_mod, name), f"{name} is exported but missing"


# --------------------------------------------------------------------------- #
# Per-type scoring — never averaged across types
# --------------------------------------------------------------------------- #


class TestPerTypeScoring:
    def test_types_land_in_separate_cells(self) -> None:
        cells = build_type_scores(
            [
                scored("speed-1", 9.5),
                scored("speed-2", 2.0),
                scored("kaggle-1", 0.4, problem_type=ProblemType.KAGGLE),
            ]
        )
        by_cell = {(c.problem_type, c.split): c for c in cells}
        assert set(by_cell) == {
            (ProblemType.SPEEDUP, Split.PRACTICE),
            (ProblemType.KAGGLE, Split.PRACTICE),
        }
        assert by_cell[(ProblemType.SPEEDUP, Split.PRACTICE)].mean_score == pytest.approx(5.75)
        assert by_cell[(ProblemType.KAGGLE, Split.PRACTICE)].mean_score == pytest.approx(0.4)

    def test_splits_land_in_separate_cells(self) -> None:
        cells = build_type_scores(
            [
                scored("speed-1", 2.0),
                scored("speed-2", 4.0, split=Split.HELD_OUT),
            ]
        )
        assert len(cells) == 2
        assert {c.split for c in cells} == {Split.PRACTICE, Split.HELD_OUT}

    def test_a_speedup_ratio_never_mixes_with_a_percentile(self) -> None:
        """The blended mean would be 3.3; no cell reports it, by construction."""
        cells = build_type_scores(
            [
                scored("speed-1", 9.5),
                scored("kaggle-1", 0.4, problem_type=ProblemType.KAGGLE),
            ]
        )
        means = sorted(c.mean_score for c in cells)
        assert means == [pytest.approx(0.4), pytest.approx(9.5)]
        blended = (9.5 + 0.4) / 2
        assert all(c.mean_score != pytest.approx(blended) for c in cells)

    def test_metrics_module_exposes_no_corpus_wide_reduction(self) -> None:
        """A blended headline number is a defect; no API here can produce one."""
        import turing.research.loop.metrics as metrics

        banned = {"overall_score", "corpus_score", "blended_score", "mean_across_types"}
        assert banned.isdisjoint(dir(metrics))

    def test_cell_ordering_is_stable(self) -> None:
        cells = build_type_scores(
            [
                scored("k1", 0.1, problem_type=ProblemType.KAGGLE, split=Split.HELD_OUT),
                scored("s1", 1.0),
                scored("k2", 0.2, problem_type=ProblemType.KAGGLE),
            ]
        )
        assert [(c.problem_type.value, c.split.value) for c in cells] == [
            ("kaggle", "held_out"),
            ("kaggle", "practice"),
            ("speedup", "practice"),
        ]

    def test_a_problem_cannot_be_counted_twice(self) -> None:
        with pytest.raises(ContractViolationError, match="scored twice"):
            build_type_scores([scored("s1", 1.0), scored("s1", 9.0)])

    def test_correctness_is_tracked_separately_from_score(self) -> None:
        cells = build_type_scores(
            [scored("s1", 9.0, correct=False), scored("s2", 1.0, correct=True)]
        )
        cell = cells[0]
        assert cell.mean_score == pytest.approx(5.0)
        assert cell.correctness_passes == 1
        assert cell.correctness_pass_rate == pytest.approx(0.5)


class TestUnscoredProblems:
    def test_unscored_problem_enters_at_the_scale_floor(self) -> None:
        problem = make_problem("speed-1")
        item = ScoredProblem.from_result(problem, None)
        assert item.scored is False
        assert item.score == pytest.approx(0.0)
        assert item.passed_correctness is False

    def test_an_unscored_speedup_does_not_beat_a_measured_failure(self) -> None:
        """Operator abandon must not raise the cell above a failed-gate round."""

        def failed(problem_id: str) -> VerificationResult:
            return VerificationResult(
                problem_id=problem_id,
                verifier_id=f"v-{problem_id}",
                score=0.0,
                passed_correctness=False,
                score_scale=SCORE_SCALE_SPEEDUP,
            )

        measured = build_type_scores(
            [
                ScoredProblem.from_result(make_problem("s1"), failed("s1")),
                ScoredProblem.from_result(make_problem("s2"), failed("s2")),
            ]
        )
        abandoned = build_type_scores(
            [
                ScoredProblem.from_result(make_problem("s1"), None),
                ScoredProblem.from_result(make_problem("s2"), None),
            ]
        )
        assert measured[0].mean_score == pytest.approx(0.0)
        assert abandoned[0].mean_score <= measured[0].mean_score

    def test_a_harness_failure_result_is_unscored_not_a_zero(self) -> None:
        problem = make_problem("speed-1")
        result = VerificationResult(
            problem_id=problem.id,
            verifier_id=problem.verifier_id,
            score=0.0,
            passed_correctness=False,
            score_scale=SCORE_SCALE_SPEEDUP,
            raw_measurements={HARNESS_FAILURE_KEY: 1.0},
        )
        item = ScoredProblem.from_result(problem, result)
        assert item.scored is False
        assert item.score == pytest.approx(0.0)
        assert item.passed_correctness is False

    def test_a_measured_zero_without_the_key_is_still_scored(self) -> None:
        problem = make_problem("speed-1")
        result = VerificationResult(
            problem_id=problem.id,
            verifier_id=problem.verifier_id,
            score=0.0,
            passed_correctness=False,
            score_scale=SCORE_SCALE_SPEEDUP,
        )
        item = ScoredProblem.from_result(problem, result)
        assert item.scored is True
        assert item.score == pytest.approx(0.0)

    def test_unknown_scale_without_a_declared_floor_is_refused(self) -> None:
        problem = make_problem("speed-1")
        with pytest.raises(ContractViolationError, match="no declared floor"):
            ScoredProblem.from_result(problem, None, score_floors={})


# --------------------------------------------------------------------------- #
# Noise floor
# --------------------------------------------------------------------------- #


class TestNoiseFloor:
    def _seed_cells(self, values: dict[int, float]) -> dict[int, tuple]:
        return {seed: build_type_scores([scored("s1", value)]) for seed, value in values.items()}

    def test_three_seeds_is_the_minimum(self) -> None:
        assert MIN_NOISE_FLOOR_SEEDS == 3
        with pytest.raises(ContractViolationError, match="at least 3 seeds"):
            measure_noise_floor(self._seed_cells({1: 1.0, 2: 1.2}))

    def test_floor_is_the_seed_spread(self) -> None:
        floors = measure_noise_floor(self._seed_cells({1: 1.0, 2: 1.2, 3: 1.4}))
        assert len(floors) == 1
        floor = floors[0]
        assert floor.stdev == pytest.approx(0.2)
        assert floor.spread_range == pytest.approx(0.4)
        assert floor.value == pytest.approx(0.2)
        assert floor.seeds == (1, 2, 3)

    def test_range_statistic_is_the_conservative_choice(self) -> None:
        floors = measure_noise_floor(
            self._seed_cells({1: 1.0, 2: 1.2, 3: 1.4}),
            statistic=NoiseFloorStatistic.RANGE,
        )
        assert floors[0].value == pytest.approx(0.4)
        assert floors[0].value > floors[0].stdev

    def test_a_cell_missing_from_one_seed_is_refused(self) -> None:
        per_seed = {
            1: build_type_scores(
                [scored("s1", 1.0), scored("k1", 0.5, problem_type=ProblemType.KAGGLE)]
            ),
            2: build_type_scores([scored("s1", 1.1)]),
            3: build_type_scores([scored("s1", 1.2)]),
        }
        with pytest.raises(ContractViolationError, match="missing from seeds"):
            measure_noise_floor(per_seed)

    def test_identical_seeds_produce_a_degenerate_floor(self) -> None:
        floors = measure_noise_floor(self._seed_cells({1: 2.0, 2: 2.0, 3: 2.0}))
        assert floors[0].value == 0.0
        assert floors[0].is_degenerate is True


# --------------------------------------------------------------------------- #
# Deltas and the refusal to guess
# --------------------------------------------------------------------------- #


class TestDeltasAndSaturation:
    def setup_method(self) -> None:
        self.parent = build_type_scores([scored("s1", 1.0), scored("s2", 1.0)])
        self.current = build_type_scores([scored("s1", 1.5), scored("s2", 1.5)])
        self.floors = floors_by_cell(
            measure_noise_floor(
                {
                    1: build_type_scores([scored("s1", 1.0), scored("s2", 1.0)]),
                    2: build_type_scores([scored("s1", 1.1), scored("s2", 1.1)]),
                    3: build_type_scores([scored("s1", 0.9), scored("s2", 0.9)]),
                }
            )
        )

    def test_gain_above_the_floor_reads_as_improving(self) -> None:
        assessments = assess_saturation(self.current, self.parent, self.floors)
        assert [a.verdict for a in assessments] == [SaturationVerdict.IMPROVING]
        assert assessments[0].gain_in_noise_units == pytest.approx(0.5 / 0.1)

    def test_gain_below_the_floor_reads_as_saturated(self) -> None:
        current = build_type_scores([scored("s1", 1.02), scored("s2", 1.02)])
        assessments = assess_saturation(current, self.parent, self.floors)
        assert assessments[0].verdict is SaturationVerdict.SATURATED
        assert "saturation candidate" in assessments[0].reason

    def test_no_noise_floor_means_no_saturation_verdict(self) -> None:
        """The headline refusal. Absence of a floor is never a licence."""
        assessments = assess_saturation(self.current, self.parent, {})
        assert len(assessments) == 1
        a = assessments[0]
        assert a.verdict is SaturationVerdict.REFUSED_NO_NOISE_FLOOR
        assert a.is_refusal is True
        assert a.noise_floor is None
        assert a.gain_in_noise_units is None
        assert a.marginal_gain == pytest.approx(0.5)  # the gain is still reported

    def test_no_noise_floor_means_no_delta_row_at_all(self) -> None:
        """A missing floor must not become ``noise_floor=0.0`` in the record."""
        deltas = compute_deltas(self.current, self.parent, {}, cost=COST)
        assert deltas == ()

    def test_degenerate_floor_is_refused_rather_than_beaten(self) -> None:
        floors = floors_by_cell(
            measure_noise_floor(
                {seed: build_type_scores([scored("s1", 1.0)]) for seed in (1, 2, 3)}
            )
        )
        current = build_type_scores([scored("s1", 1.0001)])
        parent = build_type_scores([scored("s1", 1.0)])
        assessments = assess_saturation(current, parent, floors)
        assert assessments[0].verdict is SaturationVerdict.REFUSED_DEGENERATE_NOISE_FLOOR

    def test_round_zero_has_no_delta_to_report(self) -> None:
        assessments = assess_saturation(self.current, None, self.floors)
        assert assessments[0].verdict is SaturationVerdict.REFUSED_NO_PARENT
        assert compute_deltas(self.current, None, self.floors, cost=COST) == ()

    def test_eval_set_change_suppresses_every_delta(self) -> None:
        assessments = assess_saturation(self.current, self.parent, self.floors, comparable=False)
        assert assessments[0].verdict is SaturationVerdict.REFUSED_EVAL_SET_CHANGED
        assert (
            compute_deltas(self.current, self.parent, self.floors, cost=COST, comparable=False)
            == ()
        )

    def test_deltas_are_per_cell_and_carry_their_floor(self) -> None:
        current = build_type_scores(
            [scored("s1", 1.5), scored("k1", 0.9, problem_type=ProblemType.KAGGLE)]
        )
        parent = build_type_scores(
            [scored("s1", 1.0), scored("k1", 0.9, problem_type=ProblemType.KAGGLE)]
        )
        floors = floors_by_cell(
            measure_noise_floor(
                {
                    seed: build_type_scores(
                        [
                            scored("s1", 1.0 + 0.1 * seed),
                            scored("k1", 0.5, problem_type=ProblemType.KAGGLE),
                        ]
                    )
                    for seed in (1, 2, 3)
                }
            )
        )
        deltas = compute_deltas(current, parent, floors, cost=COST)
        by_cell = {(d.problem_type, d.split): d for d in deltas}
        # Speedup has a resolved floor; kaggle's is degenerate, so it gets no
        # delta row at all rather than one that trivially "beats" zero.
        assert set(by_cell) == {(ProblemType.SPEEDUP, Split.PRACTICE)}
        assert by_cell[(ProblemType.SPEEDUP, Split.PRACTICE)].marginal_gain == pytest.approx(0.5)

    def test_a_degenerate_floor_produces_no_delta_row(self) -> None:
        """``beats_noise_floor`` against a zero floor would read as a finding."""
        floors = floors_by_cell(
            measure_noise_floor(
                {seed: build_type_scores([scored("s1", 1.0)]) for seed in (1, 2, 3)}
            )
        )
        deltas = compute_deltas(
            build_type_scores([scored("s1", 5.0)]),
            build_type_scores([scored("s1", 1.0)]),
            floors,
            cost=COST,
        )
        assert deltas == ()

    def test_a_round_that_helps_one_type_and_hurts_another_is_visible(self) -> None:
        """The exact failure averaging would hide."""
        parent = build_type_scores(
            [scored("s1", 1.0), scored("k1", 1.0, problem_type=ProblemType.KAGGLE)]
        )
        current = build_type_scores(
            [scored("s1", 2.0), scored("k1", 0.0, problem_type=ProblemType.KAGGLE)]
        )
        floors = floors_by_cell(
            measure_noise_floor(
                {
                    seed: build_type_scores(
                        [
                            scored("s1", 1.0 + 0.01 * seed),
                            scored("k1", 1.0 + 0.01 * seed, problem_type=ProblemType.KAGGLE),
                        ]
                    )
                    for seed in (1, 2, 3)
                }
            )
        )
        deltas = {
            (d.problem_type, d.split): d for d in compute_deltas(current, parent, floors, cost=COST)
        }
        assert deltas[(ProblemType.SPEEDUP, Split.PRACTICE)].marginal_gain == pytest.approx(1.0)
        assert deltas[(ProblemType.KAGGLE, Split.PRACTICE)].marginal_gain == pytest.approx(-1.0)
        # Averaged, these cancel to exactly zero and the round reads as flat.
        assert sum(d.marginal_gain for d in deltas.values()) == pytest.approx(0.0)


class TestCostPerUnitGain:
    def test_positive_gain_divides_the_round_cost(self) -> None:
        assert cost_per_unit_gain(COST, 0.5) == pytest.approx(200.0)

    def test_token_basis_uses_tokens(self) -> None:
        assert cost_per_unit_gain(COST, 0.5, basis=CostBasis.TOKENS) == pytest.approx(2000.0)

    @pytest.mark.parametrize("gain", [0.0, -0.5])
    def test_non_positive_gain_has_no_cost_per_point(self, gain: float) -> None:
        assert cost_per_unit_gain(COST, gain) is None


class TestRoundVerdict:
    def test_refusals_are_reported_and_never_summarised_away(self) -> None:
        current = build_type_scores([scored("s1", 1.5)])
        parent = build_type_scores([scored("s1", 1.0)])
        verdict = round_verdict(assess_saturation(current, parent, {}))
        assert "no noise floor measured" in verdict

    def test_an_improving_cell_is_not_deleted_by_a_sibling_refusal(self) -> None:
        """Finding 13: one refused cell must not erase an improving family's line."""
        current = build_type_scores(
            [
                scored("s1", 1.5),
                scored("k1", 0.9, problem_type=ProblemType.KAGGLE, split=Split.HELD_OUT),
            ]
        )
        parent = build_type_scores(
            [
                scored("s1", 1.0),
                scored("k1", 0.4, problem_type=ProblemType.KAGGLE, split=Split.HELD_OUT),
            ]
        )
        floors = floors_by_cell(
            measure_noise_floor(
                {
                    seed: build_type_scores([scored("s1", 1.0 + 0.1 * (seed - 2))])
                    for seed in (1, 2, 3)
                }
            )
        )
        assessments = assess_saturation(current, parent, floors)
        verdicts = {a.verdict for a in assessments}
        assert SaturationVerdict.IMPROVING in verdicts
        assert SaturationVerdict.REFUSED_NO_NOISE_FLOOR in verdicts
        verdict = round_verdict(assessments)
        assert "kaggle/held_out: no noise floor measured" in verdict
        assert "speedup/practice:" in verdict
        assert "gain" in verdict
        assert verdict.index("kaggle/held_out") < verdict.index("speedup/practice")

    def test_empty_round_says_so(self) -> None:
        assert round_verdict([]) == "no cells scored"
