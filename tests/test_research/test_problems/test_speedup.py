"""Tests for the speedup verifier.

The behaviours that matter most, in order:

1. **A fast-but-wrong workspace is rejected.** Whatever the benchmark would
   have said, a failed correctness gate scores 0.0 with
   ``passed_correctness=False`` — and the benchmark is never even run, so no
   speedup number exists to be quoted out of context later.
2. **Deleting the tests does not pass.** Exit code 0 is not enough; the pinned
   passing-test floor has to be met.
3. **The verifier never raises for a runtime condition.** A missing pinned
   reference or a benchmark that will not start comes back as a result with
   the harness-failure flag set, which is what an unattended round can escalate
   on.
4. **The verifier is frozen**, like every verifier, and cannot be relaxed by
   the thing it grades.

Every test drives a scripted runner. No real baseline is spent.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys

import pytest

from turing.research.contracts import (
    SCORE_SCALE_SPEEDUP,
    Problem,
    ProblemType,
    Split,
)
from turing.research.problems.spec import CorrectnessGate, GateCommand, OutputComparison
from turing.research.problems.speedup import (
    HARNESS_FAILURE_KEY,
    SpeedupAdapter,
    SpeedupVerifier,
)
from turing.research.problems.tolerance import Tolerance, ToleranceMode

from .conftest import (
    BENCHMARK_ARGV,
    DUMP_ARGV,
    GATE_ARGV,
    ScriptedRunner,
    by_prefix,
    command_result,
    make_spec,
)

PASSING_GATE = command_result(stdout="5 passed in 0.10s", seconds=0.1)
FAST_BENCH = command_result(seconds=1.0)


def build_verifier(spec, runner, *, harness_root, reference_root, multiplier=1.0):
    return SpeedupVerifier(
        verifier_id=f"{spec.id}.verifier",
        problem_id=spec.id,
        description=spec.gate.description,
        score_scale=SCORE_SCALE_SPEEDUP,
        spec=spec,
        harness_root=harness_root,
        reference_root=reference_root,
        python_executable=sys.executable,
        runner=runner,
        significance_multiplier=multiplier,
    )


class TestHappyPath:
    async def test_a_correct_and_faster_workspace_scores_the_ratio(
        self, harness_root, reference_root, workspace
    ):
        spec = make_spec(baseline_seconds=10.0, baseline_relative_spread=0.01)
        runner = by_prefix(
            [("pytest", PASSING_GATE), ("benchmarks/fake.py", FAST_BENCH)],
        )
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert result.passed_correctness
        assert result.score == pytest.approx(10.0)
        assert result.score_scale == SCORE_SCALE_SPEEDUP
        assert result.raw_measurements["gate_passed"] == 1.0
        assert result.raw_measurements["headroom"] == spec.headroom
        assert result.raw_measurements["candidate_runs"] == 2.0

    async def test_an_unchanged_workspace_scores_one_not_a_tiny_speedup(
        self, harness_root, reference_root, workspace
    ):
        """No measured change is 1.0, not 1.004."""
        spec = make_spec(baseline_seconds=10.0, baseline_relative_spread=0.01)
        runner = by_prefix(
            [
                ("pytest", PASSING_GATE),
                ("benchmarks/fake.py", command_result(seconds=9.96)),
            ],
        )
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert result.passed_correctness
        assert result.score == 1.0
        assert result.raw_measurements["within_noise"] == 1.0
        assert result.raw_measurements["raw_speedup"] > 1.0

    async def test_the_benchmark_is_run_the_declared_number_of_times(
        self, harness_root, reference_root, workspace
    ):
        spec = make_spec(runs=3)
        runner = by_prefix([("pytest", PASSING_GATE), ("benchmarks/fake.py", FAST_BENCH)])
        await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)
        bench_calls = [c for c in runner.calls if any("benchmarks/fake.py" in e for e in c)]
        assert len(bench_calls) == 3

    async def test_placeholders_are_resolved_before_execution(
        self, harness_root, reference_root, workspace
    ):
        spec = make_spec()
        runner = by_prefix([("pytest", PASSING_GATE), ("benchmarks/fake.py", FAST_BENCH)])
        await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)
        flattened = [element for call in runner.calls for element in call]
        assert not any("{" in element for element in flattened)
        assert any(element == sys.executable for element in flattened)
        assert any(element.startswith(str(harness_root)) for element in flattened)

    async def test_every_command_imports_from_the_workspace_not_this_checkout(
        self, harness_root, reference_root, workspace
    ):
        """Harness drivers are plain scripts; pytest's pythonpath insert never runs."""
        spec = make_spec()
        runner = by_prefix([("pytest", PASSING_GATE), ("benchmarks/fake.py", FAST_BENCH)])
        await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)
        expected = str(workspace / "src")
        assert runner.envs, "the verifier launched commands but recorded no env"
        for env in runner.envs:
            pythonpath = env["PYTHONPATH"].split(os.pathsep)
            assert pythonpath[0] == expected


class TestFastButWrongIsRejected:
    async def test_a_failing_gate_scores_zero_however_fast_the_workspace_is(
        self, harness_root, reference_root, workspace
    ):
        spec = make_spec(baseline_seconds=50.0)
        runner = by_prefix(
            [
                ("pytest", command_result(exit_code=1, stdout="1 failed, 4 passed")),
                ("benchmarks/fake.py", command_result(seconds=0.001)),
            ],
        )
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert not result.passed_correctness
        assert result.score == 0.0
        assert result.raw_measurements["gate_passed"] == 0.0
        assert HARNESS_FAILURE_KEY not in result.raw_measurements

    async def test_a_failing_gate_stops_the_benchmark_from_running_at_all(
        self, harness_root, reference_root, workspace
    ):
        """No speedup number should exist for a workspace that failed its gate."""
        spec = make_spec()
        runner = by_prefix(
            [
                ("pytest", command_result(exit_code=1, stdout="1 failed")),
                ("benchmarks/fake.py", command_result(seconds=0.001)),
            ],
        )
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert not runner.ran("benchmarks/fake.py")
        assert "raw_speedup" not in result.raw_measurements
        assert "reported_speedup" not in result.raw_measurements

    async def test_deleting_the_tests_fails_even_though_the_command_exits_zero(
        self, harness_root, reference_root, workspace
    ):
        spec = make_spec()
        runner = by_prefix(
            [
                ("pytest", command_result(stdout="1 passed in 0.01s")),
                ("benchmarks/fake.py", command_result(seconds=0.1)),
            ],
        )
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert not result.passed_correctness
        assert result.score == 0.0
        assert "below the pinned floor of 5" in result.detail
        assert not runner.ran("benchmarks/fake.py")

    async def test_a_gate_timeout_is_a_failure_not_a_fast_result(
        self, harness_root, reference_root, workspace
    ):
        spec = make_spec()
        runner = by_prefix(
            [("pytest", command_result(exit_code=-1, seconds=30.0, timed_out=True))],
            default=command_result(seconds=0.1),
        )
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)
        assert not result.passed_correctness
        assert "timed out" in result.detail


class TestArtifactComparison:
    @staticmethod
    def _spec_with_comparison(tolerance):
        return make_spec(
            gate=CorrectnessGate(
                description="suite green and the breakdown unchanged",
                commands=(
                    GateCommand(
                        argv=GATE_ARGV,
                        timeout_seconds=30.0,
                        label="pytest",
                        min_passing_tests=5,
                    ),
                    GateCommand(argv=DUMP_ARGV, timeout_seconds=30.0, label="dump"),
                ),
                comparison=OutputComparison(
                    artifact_path="breakdown.json",
                    reference_path="fake/breakdown.json",
                    tolerance=tolerance,
                ),
            )
        )

    @staticmethod
    def _runner():
        return by_prefix(
            [
                ("pytest", PASSING_GATE),
                ("fake_dump.py", command_result(seconds=0.2)),
                ("benchmarks/fake.py", FAST_BENCH),
            ]
        )

    async def test_a_matching_artifact_passes_and_the_benchmark_runs(
        self, harness_root, reference_root, workspace
    ):
        tolerance = Tolerance(mode=ToleranceMode.EXACT, rationale="bit-identical fix")
        spec = self._spec_with_comparison(tolerance)
        (reference_root / "fake").mkdir()
        (reference_root / "fake" / "breakdown.json").write_text(json.dumps({"recall": 0.5}))
        (workspace / "breakdown.json").write_text(json.dumps({"recall": 0.5}))

        runner = self._runner()
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert result.passed_correctness
        assert result.raw_measurements["comparison_passed"] == 1.0
        assert runner.ran("benchmarks/fake.py")

    async def test_a_changed_answer_is_rejected_and_never_timed(
        self, harness_root, reference_root, workspace
    ):
        """The fast-but-wrong case that a pytest exit code alone would miss."""
        tolerance = Tolerance(mode=ToleranceMode.EXACT, rationale="bit-identical fix")
        spec = self._spec_with_comparison(tolerance)
        (reference_root / "fake").mkdir()
        (reference_root / "fake" / "breakdown.json").write_text(json.dumps({"recall": 0.5}))
        (workspace / "breakdown.json").write_text(json.dumps({"recall": 0.4999999}))

        runner = self._runner()
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert not result.passed_correctness
        assert result.score == 0.0
        assert result.raw_measurements["comparison_passed"] == 0.0
        assert not runner.ran("benchmarks/fake.py")

    async def test_a_missing_artifact_is_a_gate_failure_not_a_harness_failure(
        self, harness_root, reference_root, workspace
    ):
        tolerance = Tolerance(mode=ToleranceMode.EXACT, rationale="bit-identical fix")
        spec = self._spec_with_comparison(tolerance)
        (reference_root / "fake").mkdir()
        (reference_root / "fake" / "breakdown.json").write_text(json.dumps({"recall": 0.5}))

        result = await build_verifier(
            spec, self._runner(), harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert not result.passed_correctness
        assert HARNESS_FAILURE_KEY not in result.raw_measurements
        assert "artifact unusable" in result.detail

    async def test_an_artifact_symlinked_to_the_reference_is_rejected(
        self, harness_root, reference_root, workspace
    ):
        """The cheapest possible cheat: point the artifact at the pinned answer.

        It costs one symlink, leaves every exit code green, and would make any
        workspace at all compare equal to the reference.
        """
        tolerance = Tolerance(mode=ToleranceMode.EXACT, rationale="bit-identical fix")
        spec = self._spec_with_comparison(tolerance)
        (reference_root / "fake").mkdir()
        pinned = reference_root / "fake" / "breakdown.json"
        pinned.write_text(json.dumps({"recall": 0.5}))
        (workspace / "breakdown.json").symlink_to(pinned)

        runner = self._runner()
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert not result.passed_correctness
        assert "resolves outside the workspace" in result.detail
        assert not runner.ran("benchmarks/fake.py")

    async def test_a_missing_pinned_reference_is_a_harness_failure(
        self, harness_root, reference_root, workspace
    ):
        """The agent cannot fix a missing pinned answer, so this escalates."""
        tolerance = Tolerance(mode=ToleranceMode.EXACT, rationale="bit-identical fix")
        spec = self._spec_with_comparison(tolerance)
        (workspace / "breakdown.json").write_text(json.dumps({"recall": 0.5}))

        result = await build_verifier(
            spec, self._runner(), harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert not result.passed_correctness
        assert result.raw_measurements[HARNESS_FAILURE_KEY] == 1.0
        assert "pinned reference unavailable" in result.detail

    async def test_the_reference_is_read_from_outside_the_workspace(
        self, harness_root, reference_root, workspace
    ):
        """A reference the workspace could overwrite would not be pinned at all."""
        tolerance = Tolerance(mode=ToleranceMode.EXACT, rationale="bit-identical fix")
        spec = self._spec_with_comparison(tolerance)
        (reference_root / "fake").mkdir()
        (reference_root / "fake" / "breakdown.json").write_text(json.dumps({"recall": 0.5}))
        # A forged copy planted inside the agent's write surface must be ignored.
        (workspace / "fake").mkdir()
        (workspace / "fake" / "breakdown.json").write_text(json.dumps({"recall": 999.0}))
        (workspace / "breakdown.json").write_text(json.dumps({"recall": 999.0}))

        result = await build_verifier(
            spec, self._runner(), harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert not result.passed_correctness

    async def test_an_ignored_exit_code_still_reaches_the_comparison(
        self, harness_root, reference_root, workspace
    ):
        """Maestro's report command exits non-zero every run; the artifact decides."""
        tolerance = Tolerance(mode=ToleranceMode.EXACT, rationale="bit-identical fix")
        spec = make_spec(
            gate=CorrectnessGate(
                description="report-only command, artifact decides",
                commands=(
                    GateCommand(
                        argv=DUMP_ARGV,
                        timeout_seconds=30.0,
                        label="report",
                        expect_success=False,
                    ),
                ),
                comparison=OutputComparison(
                    artifact_path="out.json",
                    reference_path="fake/out.json",
                    tolerance=tolerance,
                ),
            )
        )
        (reference_root / "fake").mkdir()
        (reference_root / "fake" / "out.json").write_text(json.dumps({"v": 1.0}))
        (workspace / "out.json").write_text(json.dumps({"v": 1.0}))

        runner = by_prefix(
            [
                ("artifacts/fake_dump.py", command_result(exit_code=1, seconds=5.0)),
                ("benchmarks/fake.py", FAST_BENCH),
            ]
        )
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert result.passed_correctness
        assert result.score > 1.0


class TestHarnessFailures:
    async def test_a_benchmark_that_will_not_run_flags_a_harness_failure(
        self, harness_root, reference_root, workspace
    ):
        spec = make_spec()
        runner = by_prefix(
            [
                ("pytest", PASSING_GATE),
                ("benchmarks/fake.py", command_result(exit_code=1, seconds=0.01)),
            ],
        )
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert not result.passed_correctness
        assert result.score == 0.0
        assert result.raw_measurements[HARNESS_FAILURE_KEY] == 1.0
        assert result.raw_measurements["gate_passed"] == 1.0

    async def test_a_gate_that_reports_no_test_count_flags_a_harness_failure(
        self, harness_root, reference_root, workspace
    ):
        """If the anti-deletion floor cannot be checked, the run is not verified."""
        spec = make_spec()
        runner = by_prefix(
            [("pytest", command_result(stdout="no summary here"))],
            default=command_result(seconds=0.1),
        )
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert not result.passed_correctness
        assert result.raw_measurements[HARNESS_FAILURE_KEY] == 1.0

    async def test_an_extra_passing_summary_fails_the_floor_not_the_harness(
        self, harness_root, reference_root, workspace
    ):
        """Workspace conftest atexit used to make the last 'N passed' the floor.

        That is a failed gate, not a broken harness: the runner did report a
        count, then the workspace printed another one. Escalating would spend
        an operator decision on a cheat.
        """
        spec = make_spec()
        runner = by_prefix(
            [
                (
                    "pytest",
                    command_result(stdout="1 passed in 0.01s\n52 passed in 0.01s"),
                ),
                ("benchmarks/fake.py", command_result(seconds=0.1)),
            ],
        )
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)

        assert not result.passed_correctness
        assert result.score == 0.0
        assert HARNESS_FAILURE_KEY not in result.raw_measurements
        assert "2 passing-test summaries" in result.detail
        assert not runner.ran("benchmarks/fake.py")

    async def test_verify_never_raises_for_a_runtime_condition(
        self, harness_root, reference_root, workspace
    ):
        """An unattended round must get a result to escalate on, not a traceback."""
        spec = make_spec()
        runner = ScriptedRunner(
            handler=lambda argv, _i: command_result(argv, exit_code=127, seconds=0.0)
        )
        result = await build_verifier(
            spec, runner, harness_root=harness_root, reference_root=reference_root
        ).verify(workspace)
        assert result.score == 0.0
        assert not result.passed_correctness


class TestVerifierIsFrozen:
    def test_attribute_assignment_is_rejected(self, harness_root, reference_root):
        verifier = build_verifier(
            make_spec(),
            ScriptedRunner(handler=lambda argv, _i: command_result(argv)),
            harness_root=harness_root,
            reference_root=reference_root,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            verifier.spec = make_spec(headroom=1000.0)  # type: ignore[misc]

    def test_binding_it_to_a_problem_succeeds(self, harness_root, reference_root):
        """Problem.__post_init__ probes the verifier behaviourally; it must survive."""
        spec = make_spec()
        verifier = build_verifier(
            spec,
            ScriptedRunner(handler=lambda argv, _i: command_result(argv)),
            harness_root=harness_root,
            reference_root=reference_root,
        )
        problem = Problem(
            id=spec.id,
            problem_type=ProblemType.SPEEDUP,
            goal=spec.goal,
            workspace_template=spec.source_root,
            verifier=verifier,
            split=Split.PRACTICE,
        )
        assert problem.verifier_id == f"{spec.id}.verifier"


@pytest.mark.slow
class TestRealSubprocessIntegration:
    """One end-to-end pass with real subprocesses, on a synthetic problem.

    Deliberately not one of the six: the corpus's baselines are 21.9 s, 24.5 s
    and 507 s, and none of them belong in a test suite. This exercises the real
    runner, the real timing harness and the real comparison against a synthetic
    1-second baseline instead.
    """

    async def test_a_real_workspace_is_gated_then_timed(
        self, harness_root, reference_root, workspace
    ):
        (harness_root / "benchmarks").mkdir()
        (harness_root / "benchmarks" / "fake.py").write_text("import time, sys\ntime.sleep(0.2)\n")
        (harness_root / "artifacts").mkdir()
        (harness_root / "artifacts" / "fake_dump.py").write_text(
            "import json, pathlib\n"
            "pathlib.Path('breakdown.json').write_text(json.dumps({'recall': 0.5}))\n"
        )
        (reference_root / "fake").mkdir()
        (reference_root / "fake" / "breakdown.json").write_text(json.dumps({"recall": 0.5}))

        spec = make_spec(
            baseline_seconds=1.0,
            baseline_relative_spread=0.01,
            gate=CorrectnessGate(
                description="synthetic gate",
                commands=(
                    GateCommand(
                        argv=("{python}", "-c", "print('5 passed in 0.01s')"),
                        timeout_seconds=30.0,
                        label="fake pytest",
                        min_passing_tests=5,
                    ),
                    GateCommand(
                        argv=("{python}", "{harness}/artifacts/fake_dump.py"),
                        timeout_seconds=30.0,
                        label="dump",
                    ),
                ),
                comparison=OutputComparison(
                    artifact_path="breakdown.json",
                    reference_path="fake/breakdown.json",
                    tolerance=Tolerance(
                        mode=ToleranceMode.EXACT, rationale="synthetic bit-identical gate"
                    ),
                ),
            ),
        )
        adapter = SpeedupAdapter(
            harness_root=harness_root, reference_root=reference_root, specs=(spec,)
        )
        assert adapter.missing_harness_scripts() == ()

        problem = adapter.load()[0]
        result = await problem.verifier.verify(workspace)

        assert result.passed_correctness
        assert result.score > 1.0
        assert result.raw_measurements["candidate_runs"] == 2.0
        assert (workspace / "breakdown.json").exists()


class TestVerifierSpecConsistency:
    def test_the_declared_benchmark_argv_is_what_gets_rendered(self):
        spec = make_spec()
        assert spec.timing.argv == BENCHMARK_ARGV
