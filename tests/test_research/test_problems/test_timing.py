"""Tests for the timing harness and the noise refusal.

The central behaviour under test: a difference that does not clear the combined
run-to-run noise band is reported as ``1.0`` — no measured change — rather than
as a small speedup. Everything else here supports that.
"""

from __future__ import annotations

import pytest

from turing.research.contracts import ContractViolationError
from turing.research.problems.timing import (
    MINIMUM_NOISE_BAND,
    SpeedupMeasurement,
    TimingHarness,
    TimingMeasurement,
    relative_spread,
)

from .conftest import ScriptedRunner, command_result


class TestRelativeSpread:
    def test_matches_the_figure_the_corpus_reports(self):
        """The two recorded problem-6 runs were reported as a 0.9% spread."""
        assert relative_spread([24.492, 24.268]) == pytest.approx(0.0092, abs=1e-4)

    def test_a_single_sample_has_no_observable_spread(self):
        assert relative_spread([1.0]) == 0.0

    def test_rejects_an_empty_sample_set(self):
        with pytest.raises(ContractViolationError):
            relative_spread([])


class TestTimingMeasurement:
    def test_reduces_with_the_median_not_the_best_run(self):
        """Best-of-N is biased low and rewards a solution that is only fast when quiet."""
        measurement = TimingMeasurement(samples=(1.0, 2.0, 9.0))
        assert measurement.median == 2.0
        assert measurement.best == 1.0
        assert measurement.worst == 9.0

    def test_is_unusable_with_a_single_run(self):
        assert not TimingMeasurement(samples=(1.0,)).ok

    def test_is_unusable_when_any_run_failed(self):
        assert not TimingMeasurement(samples=(1.0, 1.1), failures=("run 2: exit 1",)).ok

    def test_is_usable_with_two_clean_runs(self):
        assert TimingMeasurement(samples=(1.0, 1.1)).ok

    def test_rejects_a_negative_duration(self):
        with pytest.raises(ContractViolationError):
            TimingMeasurement(samples=(-1.0, 1.0))

    def test_flattens_into_float_only_measurements(self):
        data = TimingMeasurement(samples=(2.0, 4.0)).as_measurements("candidate")
        assert data["candidate_runs"] == 2.0
        assert data["candidate_median_seconds"] == 3.0
        assert all(isinstance(v, float) for v in data.values())


class TestTimingHarness:
    def test_refuses_a_single_run_at_construction(self):
        """One run reports no spread, so it cannot rule out noise."""
        with pytest.raises(ContractViolationError, match="at least 2 runs"):
            TimingHarness(
                runner=ScriptedRunner(handler=lambda argv, i: command_result(argv)), runs=1
            )

    async def test_collects_one_sample_per_run(self, tmp_path):
        runner = ScriptedRunner(
            handler=lambda argv, index: command_result(argv, seconds=1.0 + index)
        )
        harness = TimingHarness(runner=runner, runs=3)
        measurement = await harness.measure(("bench",), cwd=tmp_path, timeout_seconds=10.0)
        assert measurement.samples == (1.0, 2.0, 3.0)
        assert measurement.ok

    async def test_records_a_failed_run_and_marks_the_measurement_unusable(self, tmp_path):
        runner = ScriptedRunner(
            handler=lambda argv, index: (
                command_result(argv, seconds=1.0)
                if index == 0
                else command_result(argv, exit_code=1, seconds=0.01)
            )
        )
        harness = TimingHarness(runner=runner, runs=2)
        measurement = await harness.measure(("bench",), cwd=tmp_path, timeout_seconds=10.0)
        assert measurement.failures
        assert not measurement.ok

    async def test_discards_warmup_runs(self, tmp_path):
        runner = ScriptedRunner(
            handler=lambda argv, index: command_result(argv, seconds=10.0 if index == 0 else 1.0)
        )
        harness = TimingHarness(runner=runner, runs=2, warmup_runs=1)
        measurement = await harness.measure(("bench",), cwd=tmp_path, timeout_seconds=10.0)
        assert measurement.samples == (1.0, 1.0)
        assert len(runner.calls) == 3


class TestSpeedupMeasurementNoiseGate:
    def test_reports_a_real_speedup(self):
        speedup = SpeedupMeasurement(
            baseline_seconds=24.38,
            baseline_relative_spread=0.0092,
            candidate=TimingMeasurement(samples=(12.0, 12.1)),
        )
        assert not speedup.within_noise
        assert speedup.reported_speedup == pytest.approx(24.38 / 12.05, rel=1e-6)
        assert speedup.reported_speedup == speedup.raw_speedup

    def test_refuses_to_report_a_speedup_inside_the_noise_band(self):
        """A 0.4% difference against a 0.9% baseline spread is not a speedup."""
        speedup = SpeedupMeasurement(
            baseline_seconds=24.38,
            baseline_relative_spread=0.0092,
            candidate=TimingMeasurement(samples=(24.20, 24.42)),
        )
        assert speedup.raw_speedup > 1.0
        assert speedup.within_noise
        assert speedup.reported_speedup == 1.0
        assert "no measured change" in speedup.detail()

    def test_the_floor_applies_even_when_both_spreads_are_zero(self):
        """Two runs agreeing exactly does not license a claim below the floor.

        Degenerate spreads make the propagated band zero, at which point any
        difference at all would read as significant. The floor is what stops a
        1.002x thermal artifact from entering the corpus as a score.
        """
        speedup = SpeedupMeasurement(
            baseline_seconds=10.0,
            baseline_relative_spread=0.0,
            candidate=TimingMeasurement(samples=(9.98, 9.98)),
        )
        assert speedup.noise_band == pytest.approx(MINIMUM_NOISE_BAND)
        assert speedup.within_noise
        assert speedup.reported_speedup == 1.0

    def test_reports_a_significant_regression_honestly(self):
        """Making it worse is a real result and must not be flattened away."""
        speedup = SpeedupMeasurement(
            baseline_seconds=10.0,
            baseline_relative_spread=0.01,
            candidate=TimingMeasurement(samples=(20.0, 20.2)),
        )
        assert not speedup.within_noise
        assert speedup.reported_speedup < 1.0
        assert "regression" in speedup.detail()

    def test_an_insignificant_regression_is_also_flattened(self):
        speedup = SpeedupMeasurement(
            baseline_seconds=10.0,
            baseline_relative_spread=0.001,
            candidate=TimingMeasurement(samples=(10.01, 10.02)),
        )
        assert speedup.raw_speedup < 1.0
        assert speedup.reported_speedup == 1.0

    def test_a_stricter_multiplier_widens_the_band(self):
        candidate = TimingMeasurement(samples=(9.0, 9.1))
        lenient = SpeedupMeasurement(
            baseline_seconds=10.0, baseline_relative_spread=0.01, candidate=candidate
        )
        strict = SpeedupMeasurement(
            baseline_seconds=10.0,
            baseline_relative_spread=0.01,
            candidate=candidate,
            significance_multiplier=20.0,
        )
        assert not lenient.within_noise
        assert strict.within_noise
        assert strict.reported_speedup == 1.0

    def test_refuses_to_be_built_from_an_unusable_measurement(self):
        with pytest.raises(ContractViolationError, match="unusable timing measurement"):
            SpeedupMeasurement(
                baseline_seconds=10.0,
                baseline_relative_spread=0.01,
                candidate=TimingMeasurement(samples=(1.0,)),
            )

    def test_rejects_a_non_positive_baseline(self):
        with pytest.raises(ContractViolationError):
            SpeedupMeasurement(
                baseline_seconds=0.0,
                baseline_relative_spread=0.01,
                candidate=TimingMeasurement(samples=(1.0, 1.1)),
            )

    def test_measurements_carry_both_the_raw_and_the_reported_number(self):
        speedup = SpeedupMeasurement(
            baseline_seconds=10.0,
            baseline_relative_spread=0.001,
            candidate=TimingMeasurement(samples=(9.99, 9.99)),
        )
        data = speedup.as_measurements()
        assert data["within_noise"] == 1.0
        assert data["reported_speedup"] == 1.0
        assert data["raw_speedup"] > 1.0
        assert all(isinstance(v, float) for v in data.values())
