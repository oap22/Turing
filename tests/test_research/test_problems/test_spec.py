"""Tests for the declarative problem definitions.

Every validation here fails at *corpus load* time. That is the point: a
malformed problem definition should be caught before a round starts, not three
hours into an unattended attempt when there is nobody to notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from turing.research.contracts import ContractViolationError, Split
from turing.research.problems.spec import (
    CorrectnessGate,
    GateCommand,
    Loophole,
    LoopholeRuling,
    OutputComparison,
    SpeedupProblemSpec,
    SpreadProvenance,
    TimingSpec,
    argv_invokes_harness,
    argv_is_workspace_package_script,
)
from turing.research.problems.tolerance import Tolerance, ToleranceMode

from .conftest import make_spec

EXACT = Tolerance(mode=ToleranceMode.EXACT, rationale="the fix cannot move a value")


def _timing(**kwargs) -> TimingSpec:
    defaults = {
        "argv": ("bench",),
        "timeout_seconds": 60.0,
        "baseline_seconds": 10.0,
        "baseline_relative_spread": 0.01,
        "spread_provenance": SpreadProvenance.MEASURED,
        "measured_on": "test machine",
        "measured_at": "2026-08-12",
    }
    defaults.update(kwargs)
    return TimingSpec(**defaults)  # type: ignore[arg-type]


class TestGateCommand:
    def test_rejects_an_empty_argv(self):
        with pytest.raises(ContractViolationError):
            GateCommand(argv=(), timeout_seconds=1.0)

    def test_rejects_a_non_positive_timeout(self):
        with pytest.raises(ContractViolationError):
            GateCommand(argv=("pytest",), timeout_seconds=0.0)

    def test_rejects_a_zero_passing_floor(self):
        """Zero is what an empty suite reports, so it defeats the purpose."""
        with pytest.raises(ContractViolationError, match="must be positive"):
            GateCommand(argv=("pytest",), timeout_seconds=1.0, min_passing_tests=0)

    def test_an_ignored_exit_code_cannot_also_carry_a_passing_floor(self):
        with pytest.raises(ContractViolationError, match="exit code is ignored"):
            GateCommand(
                argv=("npm", "test"),
                timeout_seconds=1.0,
                min_passing_tests=21,
                expect_success=False,
            )


class TestCorrectnessGate:
    def test_rejects_a_gate_with_no_commands(self):
        with pytest.raises(ContractViolationError, match="passes everything"):
            CorrectnessGate(description="nothing", commands=())

    def test_rejects_a_gate_that_checks_nothing(self):
        """All exit codes ignored and no comparison means every workspace passes."""
        with pytest.raises(ContractViolationError, match="accepts any workspace"):
            CorrectnessGate(
                description="report only",
                commands=(
                    GateCommand(argv=("npm", "test"), timeout_seconds=1.0, expect_success=False),
                ),
            )

    def test_an_ignored_exit_code_is_fine_when_a_comparison_decides(self):
        gate = CorrectnessGate(
            description="report only, compared downstream",
            commands=(
                GateCommand(argv=("npm", "test"), timeout_seconds=1.0, expect_success=False),
            ),
            comparison=OutputComparison(
                artifact_path="out.json", reference_path="ref.json", tolerance=EXACT
            ),
        )
        assert gate.comparison is not None

    def test_requires_a_description(self):
        with pytest.raises(ContractViolationError, match="describe what it checks"):
            CorrectnessGate(
                description="  ",
                commands=(GateCommand(argv=("pytest",), timeout_seconds=1.0),),
            )


class TestOutputComparison:
    def test_rejects_an_absolute_artifact_path(self):
        with pytest.raises(ContractViolationError, match="relative path"):
            OutputComparison(
                artifact_path="/etc/passwd", reference_path="ref.json", tolerance=EXACT
            )

    def test_rejects_a_reference_path_that_escapes_its_root(self):
        with pytest.raises(ContractViolationError, match="stays inside its root"):
            OutputComparison(
                artifact_path="out.json",
                reference_path="../../answers/ref.json",
                tolerance=EXACT,
            )

    def test_rejects_an_empty_path(self):
        with pytest.raises(ContractViolationError):
            OutputComparison(artifact_path="", reference_path="ref.json", tolerance=EXACT)


class TestTimingSpec:
    def test_rejects_a_single_run(self):
        with pytest.raises(ContractViolationError, match="at least 2 runs"):
            _timing(runs=1)

    def test_rejects_a_timeout_below_the_baseline(self):
        """The unmodified workspace would be killed and score as broken."""
        with pytest.raises(ContractViolationError, match=r"below the .* baseline"):
            _timing(baseline_seconds=100.0, timeout_seconds=30.0)

    def test_rejects_a_non_positive_baseline(self):
        with pytest.raises(ContractViolationError):
            _timing(baseline_seconds=0.0)

    def test_requires_measurement_provenance(self):
        with pytest.raises(ContractViolationError, match="machine and date"):
            _timing(measured_on="  ")

    def test_recorded_samples_must_match_the_declared_spread(self):
        with pytest.raises(ContractViolationError, match="does not match"):
            _timing(
                baseline_seconds=24.38,
                baseline_relative_spread=0.2,
                baseline_samples=(24.492, 24.268),
            )

    def test_recorded_samples_force_measured_provenance(self):
        with pytest.raises(ContractViolationError, match="MEASURED"):
            _timing(
                baseline_seconds=24.38,
                baseline_relative_spread=0.0092,
                baseline_samples=(24.492, 24.268),
                spread_provenance=SpreadProvenance.ASSUMED,
            )

    def test_one_recorded_sample_is_not_a_spread(self):
        with pytest.raises(ContractViolationError, match="not a spread"):
            _timing(baseline_samples=(10.0,))

    def test_the_measured_problem_six_baseline_validates(self):
        spec = _timing(
            baseline_seconds=24.38,
            timeout_seconds=300.0,
            baseline_relative_spread=0.0092,
            baseline_samples=(24.492, 24.268),
        )
        assert spec.spread_provenance is SpreadProvenance.MEASURED


class TestLoophole:
    def test_requires_a_detection_hint(self):
        with pytest.raises(ContractViolationError, match="detection hint"):
            Loophole(
                id="x",
                description="something",
                ruling=LoopholeRuling.UNDECIDED,
                detection_hint="  ",
            )

    def test_requires_an_id_and_description(self):
        with pytest.raises(ContractViolationError):
            Loophole(
                id="",
                description="something",
                ruling=LoopholeRuling.ALLOWED,
                detection_hint="read the diff",
            )


class TestSpeedupProblemSpec:
    def test_rejects_headroom_that_is_not_a_speedup(self):
        with pytest.raises(ContractViolationError, match="not a speedup"):
            make_spec(headroom=1.0)

    def test_rejects_duplicate_loophole_ids(self):
        base = make_spec()
        duplicate = Loophole(
            id="delete-or-skip-tests",
            description="again",
            ruling=LoopholeRuling.UNDECIDED,
            detection_hint="read the diff",
        )
        with pytest.raises(ContractViolationError, match="duplicate loophole"):
            SpeedupProblemSpec(
                id=base.id,
                goal=base.goal,
                target=base.target,
                source_root=base.source_root,
                timing=base.timing,
                gate=base.gate,
                split=base.split,
                headroom=base.headroom,
                loopholes=(*base.loopholes, duplicate),
            )

    def test_requires_a_target(self):
        with pytest.raises(ContractViolationError, match="name what to make faster"):
            SpeedupProblemSpec(
                id="x",
                goal="faster",
                target="  ",
                source_root=Path("/tmp"),
                timing=_timing(),
                gate=CorrectnessGate(
                    description="green",
                    commands=(GateCommand(argv=("pytest",), timeout_seconds=1.0),),
                ),
                split=Split.PRACTICE,
                headroom=2.0,
            )

    def test_rejects_a_timing_driver_that_does_not_live_under_harness(self):
        """The confirmed speedup-04 exploit: `npm test` from the workspace cwd."""
        base = make_spec()
        with pytest.raises(ContractViolationError, match="under \\{harness\\}"):
            SpeedupProblemSpec(
                id=base.id,
                goal=base.goal,
                target=base.target,
                source_root=base.source_root,
                timing=_timing(argv=("npm", "test", "--", "packages/core/src/retry.test.ts")),
                gate=base.gate,
                split=base.split,
                headroom=base.headroom,
            )

    def test_rejects_a_gate_command_that_runs_npm_test_from_the_workspace(self):
        base = make_spec()
        with pytest.raises(ContractViolationError, match="package-manager"):
            SpeedupProblemSpec(
                id=base.id,
                goal=base.goal,
                target=base.target,
                source_root=base.source_root,
                timing=base.timing,
                gate=CorrectnessGate(
                    description="report from npm",
                    commands=(
                        GateCommand(
                            argv=("npm", "test", "--", "--reporter=json"),
                            timeout_seconds=1.0,
                            expect_success=False,
                        ),
                    ),
                    comparison=OutputComparison(
                        artifact_path="out.json",
                        reference_path="ref.json",
                        tolerance=EXACT,
                    ),
                ),
                split=base.split,
                headroom=base.headroom,
            )

    def test_undecided_loopholes_are_queryable(self):
        base = make_spec()
        spec = SpeedupProblemSpec(
            id=base.id,
            goal=base.goal,
            target=base.target,
            source_root=base.source_root,
            timing=base.timing,
            gate=base.gate,
            split=base.split,
            headroom=base.headroom,
            loopholes=(
                *base.loopholes,
                Loophole(
                    id="hardcode-constants",
                    description="hardcode instead of exporting",
                    ruling=LoopholeRuling.UNDECIDED,
                    detection_hint="diff the shared package",
                ),
            ),
        )
        assert [lh.id for lh in spec.undecided_loopholes] == ["hardcode-constants"]

    def test_is_held_out_reflects_the_split(self):
        assert make_spec(split=Split.HELD_OUT).is_held_out
        assert not make_spec(split=Split.PRACTICE).is_held_out


class TestFingerprintMaterial:
    def test_is_stable_across_identical_specs(self):
        assert make_spec().fingerprint_material() == make_spec().fingerprint_material()

    def test_moves_when_the_baseline_is_repinned(self):
        assert (
            make_spec(baseline_seconds=10.0).fingerprint_material()
            != make_spec(baseline_seconds=11.0).fingerprint_material()
        )

    def test_moves_when_the_split_changes(self):
        assert (
            make_spec(split=Split.PRACTICE).fingerprint_material()
            != make_spec(split=Split.HELD_OUT).fingerprint_material()
        )

    def test_moves_when_a_tolerance_is_loosened(self):
        """A quietly relaxed gate must read as a different eval set, not an improvement."""
        tight = make_spec(
            gate=CorrectnessGate(
                description="compare",
                commands=(GateCommand(argv=("pytest",), timeout_seconds=1.0),),
                comparison=OutputComparison(
                    artifact_path="out.json",
                    reference_path="ref.json",
                    tolerance=Tolerance(
                        mode=ToleranceMode.RELATIVE, rationale="measured", rtol=1e-9
                    ),
                ),
            )
        )
        loose = make_spec(
            gate=CorrectnessGate(
                description="compare",
                commands=(GateCommand(argv=("pytest",), timeout_seconds=1.0),),
                comparison=OutputComparison(
                    artifact_path="out.json",
                    reference_path="ref.json",
                    tolerance=Tolerance(
                        mode=ToleranceMode.RELATIVE, rationale="measured", rtol=1e-1
                    ),
                ),
            )
        )
        assert tight.fingerprint_material() != loose.fingerprint_material()

    def test_moves_when_an_anti_deletion_floor_is_lowered(self):
        def spec_with_floor(floor: int) -> SpeedupProblemSpec:
            return make_spec(
                gate=CorrectnessGate(
                    description="green",
                    commands=(
                        GateCommand(argv=("pytest",), timeout_seconds=1.0, min_passing_tests=floor),
                    ),
                )
            )

        assert (
            spec_with_floor(52).fingerprint_material() != spec_with_floor(1).fingerprint_material()
        )

    def test_moves_when_the_source_repo_changes(self):
        """A different checkout is a different eval set, not the same problems elsewhere."""
        assert (
            make_spec(source_root=Path("/repo-a")).fingerprint_material()
            != make_spec(source_root=Path("/repo-b")).fingerprint_material()
        )

    def test_moves_when_workspace_excludes_change(self):
        """Excluding tests from the copy is a different eval set, not a quieter workspace."""
        assert (
            make_spec(workspace_excludes=(".git",)).fingerprint_material()
            != make_spec(workspace_excludes=(".git", "tests")).fingerprint_material()
        )

    def test_moves_when_the_timing_timeout_changes(self):
        """A 45× timeout is a different measurement, not the same benchmark with more patience."""
        assert (
            make_spec(timeout_seconds=60.0).fingerprint_material()
            != make_spec(timeout_seconds=2700.0).fingerprint_material()
        )


class TestArgvOwnership:
    def test_a_harness_path_is_the_ownership_marker(self):
        assert argv_invokes_harness(("{python}", "{harness}/benchmarks/x.py"))
        assert not argv_invokes_harness(("npm", "test"))

    def test_npm_test_from_the_workspace_is_a_package_script(self):
        assert argv_is_workspace_package_script(("npm", "test", "--", "retry.test.ts"))

    def test_a_harness_wrapper_around_the_same_binary_is_not(self):
        assert not argv_is_workspace_package_script(
            ("{harness}/bin/npm", "test", "--", "retry.test.ts")
        )
