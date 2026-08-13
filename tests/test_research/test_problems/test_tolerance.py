"""Tests for the tolerance modes.

Each mode is tested against the measurement that made it necessary. The
important cases are the two-sided ones: the intended fix must *pass* and a
plausible wrong answer must *fail*. A tolerance that only rejects is a
benchmark nobody can solve; one that only accepts is not a gate.
"""

from __future__ import annotations

import pytest

from turing.research.contracts import ContractViolationError
from turing.research.problems.tolerance import (
    ComparisonOutcome,
    Tolerance,
    ToleranceMode,
    compare,
)


def _tolerance(mode: ToleranceMode, **kwargs: float | str | int) -> Tolerance:
    return Tolerance(mode=mode, rationale="because the measurement said so", **kwargs)  # type: ignore[arg-type]


class TestToleranceValidation:
    def test_requires_a_rationale(self):
        with pytest.raises(ContractViolationError, match="why it is set"):
            Tolerance(mode=ToleranceMode.EXACT, rationale="   ")

    def test_exact_admits_no_slack(self):
        with pytest.raises(ContractViolationError, match="EXACT admits no tolerance"):
            _tolerance(ToleranceMode.EXACT, rtol=1e-9)

    def test_relative_needs_a_tolerance(self):
        with pytest.raises(ContractViolationError, match="non-zero rtol or atol"):
            _tolerance(ToleranceMode.RELATIVE)

    def test_top_k_needs_a_score_tolerance(self):
        with pytest.raises(ContractViolationError, match="declare the score tolerance"):
            _tolerance(ToleranceMode.TOP_K_SEQUENCE)

    def test_cosine_needs_a_threshold_in_range(self):
        with pytest.raises(ContractViolationError, match="min_cosine in"):
            _tolerance(ToleranceMode.COSINE)
        with pytest.raises(ContractViolationError, match="min_cosine in"):
            _tolerance(ToleranceMode.COSINE, min_cosine=1.5)

    def test_min_cosine_is_rejected_on_other_modes(self):
        with pytest.raises(ContractViolationError, match="only applies to COSINE"):
            _tolerance(ToleranceMode.RELATIVE, rtol=1e-6, min_cosine=0.99)

    def test_test_outcome_set_needs_a_scope_and_a_floor(self):
        with pytest.raises(ContractViolationError, match="fully green"):
            _tolerance(ToleranceMode.TEST_OUTCOME_SET, min_all_pass_count=21)
        with pytest.raises(ContractViolationError, match="floor on how many tests"):
            _tolerance(ToleranceMode.TEST_OUTCOME_SET, all_pass_scope="a.test.ts")

    def test_scope_fields_are_rejected_on_other_modes(self):
        with pytest.raises(ContractViolationError, match="only apply to TEST_OUTCOME_SET"):
            _tolerance(ToleranceMode.EXACT, all_pass_scope="a.test.ts")

    def test_negative_tolerances_are_rejected(self):
        with pytest.raises(ContractViolationError, match="cannot be negative"):
            _tolerance(ToleranceMode.RELATIVE, rtol=-1e-6)


class TestExactMode:
    """Problems 1 and 3: the intended fix cannot move a single value."""

    def test_identical_structures_pass(self):
        payload = {"recall": 0.8137254901960784, "precision": [0.5, 0.25]}
        outcome = compare(_tolerance(ToleranceMode.EXACT), payload, dict(payload))
        assert outcome.passed
        assert outcome.worst_deviation == 0.0

    def test_a_one_ulp_difference_fails(self):
        outcome = compare(
            _tolerance(ToleranceMode.EXACT),
            {"recall": 0.8137254901960785},
            {"recall": 0.8137254901960784},
        )
        assert not outcome.passed
        assert "recall" in outcome.detail

    def test_a_changed_label_fails(self):
        outcome = compare(
            _tolerance(ToleranceMode.EXACT),
            {"label": "b", "value": 1.0},
            {"label": "a", "value": 1.0},
        )
        assert not outcome.passed

    def test_a_shape_change_fails_rather_than_raising(self):
        outcome = compare(_tolerance(ToleranceMode.EXACT), {"recall": 1.0}, {"recall": [1.0]})
        assert not outcome.passed
        assert "shape" in outcome.detail

    def test_a_missing_key_fails(self):
        outcome = compare(_tolerance(ToleranceMode.EXACT), {}, {"recall": 1.0})
        assert not outcome.passed
        assert "missing keys" in outcome.detail

    def test_a_boolean_is_not_a_number(self):
        outcome = compare(_tolerance(ToleranceMode.EXACT), {"v": True}, {"v": 1.0})
        assert not outcome.passed


class TestRelativeMode:
    def test_a_difference_inside_the_tolerance_passes(self):
        outcome = compare(
            _tolerance(ToleranceMode.RELATIVE, rtol=1e-3),
            {"loss": 0.10005},
            {"loss": 0.1},
        )
        assert outcome.passed

    def test_a_difference_outside_the_tolerance_fails(self):
        outcome = compare(
            _tolerance(ToleranceMode.RELATIVE, rtol=1e-6),
            {"loss": 0.11},
            {"loss": 0.1},
        )
        assert not outcome.passed
        assert outcome.worst_deviation == pytest.approx(0.1, rel=1e-6)


class TestTopKSequenceMode:
    """Problem 2's measured trap: paths identical, scores drifting ~1e-7."""

    @staticmethod
    def _reference():
        return [
            {"labels": ["notes/a.md", "notes/b.md"], "scores": [0.91, 0.73]},
            {"labels": ["notes/c.md", "notes/a.md"], "scores": [0.88, 0.51]},
        ]

    def test_the_measured_score_drift_passes(self):
        candidate = [
            {"labels": ["notes/a.md", "notes/b.md"], "scores": [0.9100001, 0.7299999]},
            {"labels": ["notes/c.md", "notes/a.md"], "scores": [0.8800001, 0.5099999]},
        ]
        outcome = compare(
            _tolerance(ToleranceMode.TOP_K_SEQUENCE, atol=1e-6), candidate, self._reference()
        )
        assert outcome.passed, outcome.detail

    def test_a_reordered_ranking_fails_even_with_identical_scores(self):
        """Comparing paths as a set would let this through; the ranking is the answer."""
        candidate = [
            {"labels": ["notes/b.md", "notes/a.md"], "scores": [0.91, 0.73]},
            {"labels": ["notes/c.md", "notes/a.md"], "scores": [0.88, 0.51]},
        ]
        outcome = compare(
            _tolerance(ToleranceMode.TOP_K_SEQUENCE, atol=1e-6), candidate, self._reference()
        )
        assert not outcome.passed
        assert "ranking is the answer" in outcome.detail

    def test_a_different_path_fails(self):
        candidate = [
            {"labels": ["notes/z.md", "notes/b.md"], "scores": [0.91, 0.73]},
            {"labels": ["notes/c.md", "notes/a.md"], "scores": [0.88, 0.51]},
        ]
        outcome = compare(
            _tolerance(ToleranceMode.TOP_K_SEQUENCE, atol=1e-6), candidate, self._reference()
        )
        assert not outcome.passed

    def test_a_score_drift_beyond_the_tolerance_fails(self):
        candidate = [
            {"labels": ["notes/a.md", "notes/b.md"], "scores": [0.92, 0.73]},
            {"labels": ["notes/c.md", "notes/a.md"], "scores": [0.88, 0.51]},
        ]
        outcome = compare(
            _tolerance(ToleranceMode.TOP_K_SEQUENCE, atol=1e-6), candidate, self._reference()
        )
        assert not outcome.passed

    def test_a_truncated_result_set_fails(self):
        outcome = compare(
            _tolerance(ToleranceMode.TOP_K_SEQUENCE, atol=1e-6),
            self._reference()[:1],
            self._reference(),
        )
        assert not outcome.passed

    def test_a_malformed_record_fails_rather_than_grading_nothing(self):
        outcome = compare(
            _tolerance(ToleranceMode.TOP_K_SEQUENCE, atol=1e-6),
            [{"paths": ["notes/a.md"], "scores": [0.91]}],
            [{"labels": ["notes/a.md"], "scores": [0.91]}],
        )
        assert not outcome.passed
        assert "missing" in outcome.detail


class TestCosineMode:
    """Problem 5: padding shifts the vectors, so equality is the wrong bar."""

    def test_a_small_perturbation_passes(self):
        reference = [[1.0, 2.0, 3.0], [0.5, 0.5, 0.5]]
        candidate = [[1.0001, 2.0002, 2.9999], [0.5001, 0.4999, 0.5]]
        outcome = compare(_tolerance(ToleranceMode.COSINE, min_cosine=0.9999), candidate, reference)
        assert outcome.passed, outcome.detail

    def test_a_rescaled_vector_passes_because_direction_is_what_matters(self):
        outcome = compare(
            _tolerance(ToleranceMode.COSINE, min_cosine=0.9999),
            [[2.0, 4.0, 6.0]],
            [[1.0, 2.0, 3.0]],
        )
        assert outcome.passed

    def test_a_degenerate_zero_vector_fails(self):
        """Returning zeros would be the cheapest 'fast' answer if this passed."""
        outcome = compare(
            _tolerance(ToleranceMode.COSINE, min_cosine=0.9999),
            [[0.0, 0.0, 0.0]],
            [[1.0, 2.0, 3.0]],
        )
        assert not outcome.passed
        assert "undefined" in outcome.detail

    def test_a_meaningfully_rotated_vector_fails(self):
        outcome = compare(
            _tolerance(ToleranceMode.COSINE, min_cosine=0.9999),
            [[3.0, 2.0, 1.0]],
            [[1.0, 2.0, 3.0]],
        )
        assert not outcome.passed

    def test_named_vectors_are_matched_by_key(self):
        outcome = compare(
            _tolerance(ToleranceMode.COSINE, min_cosine=0.9999),
            {"a": [1.0, 0.0], "b": [0.0, 1.0]},
            {"a": [1.0, 0.0], "b": [0.0, 1.0]},
        )
        assert outcome.passed

    def test_a_missing_vector_fails(self):
        outcome = compare(
            _tolerance(ToleranceMode.COSINE, min_cosine=0.9999),
            {"a": [1.0, 0.0]},
            {"a": [1.0, 0.0], "b": [0.0, 1.0]},
        )
        assert not outcome.passed


class TestTestOutcomeSetMode:
    """Problem 4: the correct fix changes the pass/fail set, on purpose."""

    SCOPE = "packages/core/src/retry.test.ts"

    def _tolerance(self):
        return Tolerance(
            mode=ToleranceMode.TEST_OUTCOME_SET,
            rationale="the correct fix flips three failing tests to passing",
            all_pass_scope=self.SCOPE,
            min_all_pass_count=3,
        )

    def _reference(self):
        return {
            "files": {
                self.SCOPE: {"t1": "passed", "t2": "failed", "t3": "failed"},
                "packages/core/src/other.test.ts": {"o1": "passed", "o2": "failed"},
            }
        }

    def test_the_intended_fix_passes_even_though_outcomes_changed(self):
        candidate = {
            "files": {
                self.SCOPE: {"t1": "passed", "t2": "passed", "t3": "passed"},
                "packages/core/src/other.test.ts": {"o1": "passed", "o2": "failed"},
            }
        }
        outcome = compare(self._tolerance(), candidate, self._reference())
        assert outcome.passed, outcome.detail

    def test_a_still_failing_scoped_test_fails(self):
        candidate = {
            "files": {
                self.SCOPE: {"t1": "passed", "t2": "passed", "t3": "failed"},
                "packages/core/src/other.test.ts": {"o1": "passed", "o2": "failed"},
            }
        }
        assert not compare(self._tolerance(), candidate, self._reference()).passed

    def test_deleting_scoped_tests_fails_the_floor(self):
        candidate = {
            "files": {
                self.SCOPE: {"t1": "passed"},
                "packages/core/src/other.test.ts": {"o1": "passed", "o2": "failed"},
            }
        }
        outcome = compare(self._tolerance(), candidate, self._reference())
        assert not outcome.passed
        assert "removed rather than fixed" in outcome.detail

    def test_replacing_scoped_tests_with_the_same_number_of_empty_ones_fails(self):
        """The confirmed cheat: 21 empty tests used to satisfy the count floor."""
        candidate = {
            "files": {
                self.SCOPE: {"empty1": "passed", "empty2": "passed", "empty3": "passed"},
                "packages/core/src/other.test.ts": {"o1": "passed", "o2": "failed"},
            }
        }
        outcome = compare(self._tolerance(), candidate, self._reference())
        assert not outcome.passed
        assert "pinned test(s)" in outcome.detail
        assert "empty1" not in outcome.detail

    def test_extra_passing_tests_in_the_scoped_file_are_allowed(self):
        """The intended fix may add tests; it must not have to be a closed set."""
        candidate = {
            "files": {
                self.SCOPE: {
                    "t1": "passed",
                    "t2": "passed",
                    "t3": "passed",
                    "t4_new": "passed",
                },
                "packages/core/src/other.test.ts": {"o1": "passed", "o2": "failed"},
            }
        }
        outcome = compare(self._tolerance(), candidate, self._reference())
        assert outcome.passed, outcome.detail

    def test_a_regression_in_another_file_fails(self):
        candidate = {
            "files": {
                self.SCOPE: {"t1": "passed", "t2": "passed", "t3": "passed"},
                "packages/core/src/other.test.ts": {"o1": "failed", "o2": "failed"},
            }
        }
        outcome = compare(self._tolerance(), candidate, self._reference())
        assert not outcome.passed
        assert "moved" in outcome.detail

    def test_a_pre_existing_failure_elsewhere_is_allowed_to_stay_failing(self):
        """The Maestro baseline is not green; a still-red test is not a regression."""
        candidate = {
            "files": {
                self.SCOPE: {"t1": "passed", "t2": "passed", "t3": "passed"},
                "packages/core/src/other.test.ts": {"o1": "passed", "o2": "failed"},
            }
        }
        assert compare(self._tolerance(), candidate, self._reference()).passed

    def test_deleting_another_test_file_fails(self):
        candidate = {"files": {self.SCOPE: {"t1": "passed", "t2": "passed", "t3": "passed"}}}
        outcome = compare(self._tolerance(), candidate, self._reference())
        assert not outcome.passed
        assert "absent" in outcome.detail

    def test_the_scoped_file_disappearing_fails(self):
        candidate = {"files": {"packages/core/src/other.test.ts": {"o1": "passed"}}}
        outcome = compare(self._tolerance(), candidate, self._reference())
        assert not outcome.passed

    def test_a_malformed_report_fails(self):
        outcome = compare(self._tolerance(), {"nope": 1}, self._reference())
        assert not outcome.passed


class TestComparisonOutcome:
    def test_flattens_into_float_only_measurements(self):
        data = ComparisonOutcome(
            passed=True, mode=ToleranceMode.EXACT, detail="ok", worst_deviation=0.5
        ).as_measurements()
        assert data == {"comparison_passed": 1.0, "comparison_worst_deviation": 0.5}
