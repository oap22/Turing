"""Tests for the six-problem speedup corpus.

These assert the *measured* facts from the 2026-08-12 profiling sweep and,
more importantly, the gate traps derived from them. Each trap has a test that
would fail if someone "tidied" the tolerance into something simpler and wrong.

No baseline is run here. The numbers are pinned data, and re-deriving them is
exactly what the design forbids.
"""

from __future__ import annotations

import pytest

from turing.research.contracts import Split
from turing.research.problems.catalog import (
    DEFAULT_MAESTRO_REPO,
    DEFAULT_SPLITS,
    DEFAULT_TURING_REPO,
    speedup_specs,
)
from turing.research.problems.spec import (
    LoopholeRuling,
    SpreadProvenance,
    argv_invokes_harness,
    argv_is_workspace_package_script,
)
from turing.research.problems.timing import MINIMUM_RUNS, relative_spread
from turing.research.problems.tolerance import ToleranceMode

SPECS = {spec.id: spec for spec in speedup_specs()}

EXPECTED_IDS = (
    "speedup-01-claim-scorer",
    "speedup-02-vault-index-query",
    "speedup-03-hash-embedder",
    "speedup-04-maestro-retry",
    "speedup-05-embedding-model-config",
    "speedup-06-replay-window-evict",
)


class TestCorpusShape:
    def test_the_speedup_family_has_exactly_six_problems(self):
        assert tuple(SPECS) == EXPECTED_IDS

    def test_ids_are_unique(self):
        assert len(SPECS) == len(EXPECTED_IDS)

    def test_every_problem_declares_a_target_and_a_goal(self):
        for spec in SPECS.values():
            assert spec.target
            assert spec.goal

    def test_every_problem_has_measured_headroom_above_one(self):
        for spec in SPECS.values():
            assert spec.headroom > 1.0, spec.id

    def test_the_split_is_four_practice_and_two_held_out(self):
        practice = [s for s in SPECS.values() if s.split is Split.PRACTICE]
        held_out = [s for s in SPECS.values() if s.split is Split.HELD_OUT]
        assert len(practice) == 4
        assert len(held_out) == 2

    def test_the_bug_fix_outlier_stays_in_practice(self):
        """Holding out the only instance of a capability measures nothing about it."""
        assert SPECS["speedup-04-maestro-retry"].split is Split.PRACTICE

    def test_the_split_is_a_parameter_not_a_hardcoded_decision(self):
        overridden = {
            s.id: s.split
            for s in speedup_specs(split_overrides={"speedup-01-claim-scorer": Split.HELD_OUT})
        }
        assert overridden["speedup-01-claim-scorer"] is Split.HELD_OUT
        assert DEFAULT_SPLITS["speedup-01-claim-scorer"] is Split.PRACTICE


class TestPinnedBaselines:
    @pytest.mark.parametrize(
        ("problem_id", "baseline", "headroom"),
        [
            ("speedup-01-claim-scorer", 21.9, 9.5),
            ("speedup-02-vault-index-query", 5.27, 25.5),
            ("speedup-03-hash-embedder", 7.47, 7.2),
            ("speedup-04-maestro-retry", 507.0, 500.0),
            ("speedup-05-embedding-model-config", 4.33, 5.9),
            ("speedup-06-replay-window-evict", 24.38, 2.0),
        ],
    )
    def test_matches_the_2026_08_12_measurement(self, problem_id, baseline, headroom):
        spec = SPECS[problem_id]
        assert spec.timing.baseline_seconds == pytest.approx(baseline)
        assert spec.headroom == pytest.approx(headroom)

    def test_every_baseline_records_its_machine_and_date(self):
        for spec in SPECS.values():
            assert "M4 Pro" in spec.timing.measured_on
            assert spec.timing.measured_at == "2026-08-12"

    def test_problem_six_carries_the_two_runs_it_was_measured_from(self):
        timing = SPECS["speedup-06-replay-window-evict"].timing
        assert timing.baseline_samples == (24.492, 24.268)
        assert timing.spread_provenance is SpreadProvenance.MEASURED
        assert relative_spread(timing.baseline_samples) == pytest.approx(
            timing.baseline_relative_spread, abs=1e-3
        )

    def test_problem_one_records_its_measured_spread(self):
        timing = SPECS["speedup-01-claim-scorer"].timing
        assert timing.spread_provenance is SpreadProvenance.MEASURED
        assert timing.baseline_relative_spread == pytest.approx(0.009)

    def test_unrecorded_spreads_are_marked_assumed_not_measured(self):
        """A conservative default must never be readable later as a measurement."""
        assumed = [
            s.id for s in SPECS.values() if s.timing.spread_provenance is SpreadProvenance.ASSUMED
        ]
        assert set(assumed) == {
            "speedup-02-vault-index-query",
            "speedup-03-hash-embedder",
            "speedup-04-maestro-retry",
            "speedup-05-embedding-model-config",
        }

    def test_every_timing_spec_repeats_and_leaves_room_for_the_baseline(self):
        for spec in SPECS.values():
            assert spec.timing.runs >= MINIMUM_RUNS, spec.id
            assert spec.timing.timeout_seconds >= spec.timing.baseline_seconds, spec.id


class TestGateTraps:
    def test_trap_one_vault_index_gates_on_the_path_sequence_not_exact_scores(self):
        """Measured: vectorising drifts the scores ~1e-7 with identical top-k paths."""
        comparison = SPECS["speedup-02-vault-index-query"].gate.comparison
        assert comparison is not None
        assert comparison.tolerance.mode is ToleranceMode.TOP_K_SEQUENCE
        assert comparison.tolerance.atol == pytest.approx(1e-6)
        assert "1e-7" in comparison.tolerance.rationale

    def test_trap_two_embeddings_gate_on_cosine_never_equality(self):
        comparison = SPECS["speedup-05-embedding-model-config"].gate.comparison
        assert comparison is not None
        assert comparison.tolerance.mode is ToleranceMode.COSINE
        assert comparison.tolerance.min_cosine == pytest.approx(0.9999)
        assert "padding" in comparison.tolerance.rationale

    def test_trap_three_maestro_does_not_gate_on_an_unchanged_pass_fail_set(self):
        """The correct fix flips three failing tests to passing, on purpose."""
        comparison = SPECS["speedup-04-maestro-retry"].gate.comparison
        assert comparison is not None
        tolerance = comparison.tolerance
        assert tolerance.mode is ToleranceMode.TEST_OUTCOME_SET
        assert tolerance.all_pass_scope == "packages/core/src/retry.test.ts"
        assert tolerance.min_all_pass_count == 21

    def test_trap_three_maestros_report_command_ignores_its_exit_code(self):
        """Its suite has 18 pre-existing failures, so it exits non-zero every run."""
        commands = SPECS["speedup-04-maestro-retry"].gate.commands
        assert commands[0].expect_success is False
        assert commands[0].min_passing_tests is None

    def test_trap_four_maestros_hardcoding_loophole_is_recorded_and_undecided(self):
        spec = SPECS["speedup-04-maestro-retry"]
        undecided = {lh.id for lh in spec.undecided_loopholes}
        assert "hardcoded-retry-constants" in undecided
        loophole = next(lh for lh in spec.loopholes if lh.id == "hardcoded-retry-constants")
        assert loophole.ruling is LoopholeRuling.UNDECIDED
        assert "@maestro/shared" in loophole.description

    def test_trap_five_bit_identical_problems_use_exact_equality(self):
        for problem_id in ("speedup-01-claim-scorer", "speedup-03-hash-embedder"):
            comparison = SPECS[problem_id].gate.comparison
            assert comparison is not None, problem_id
            assert comparison.tolerance.mode is ToleranceMode.EXACT, problem_id

    def test_trap_six_every_timing_measurement_repeats(self):
        for spec in SPECS.values():
            assert spec.timing.runs >= 2, spec.id

    def test_every_tolerance_records_why_it_is_what_it_is(self):
        for spec in SPECS.values():
            comparison = spec.gate.comparison
            if comparison is not None:
                assert len(comparison.tolerance.rationale) > 40, spec.id


class TestAntiCheatFloors:
    @pytest.mark.parametrize(
        ("problem_id", "floor"),
        [
            ("speedup-01-claim-scorer", 41),
            ("speedup-02-vault-index-query", 9),
            ("speedup-03-hash-embedder", 61),
            ("speedup-05-embedding-model-config", 14),
            ("speedup-06-replay-window-evict", 52),
        ],
    )
    def test_pytest_gated_problems_pin_their_passing_test_count(self, problem_id, floor):
        commands = SPECS[problem_id].gate.commands
        assert commands[0].min_passing_tests == floor

    def test_every_problem_records_the_test_deletion_loophole(self):
        for spec in SPECS.values():
            assert any(lh.id == "delete-or-skip-tests" for lh in spec.loopholes), spec.id

    def test_maestro_covers_deletion_through_the_outcome_set_instead_of_a_floor(self):
        spec = SPECS["speedup-04-maestro-retry"]
        loophole = next(lh for lh in spec.loopholes if lh.id == "delete-or-skip-tests")
        assert "TEST_OUTCOME_SET" in loophole.detection_hint
        assert "pinned" in loophole.detection_hint

    def test_only_maestro_has_an_undecided_ruling(self):
        undecided = {spec.id for spec in SPECS.values() if spec.undecided_loopholes}
        assert undecided == {"speedup-04-maestro-retry"}


class TestSourceRoots:
    def test_five_problems_come_from_the_turing_repo_and_one_from_maestro(self):
        from_maestro = [s.id for s in SPECS.values() if s.source_root == DEFAULT_MAESTRO_REPO]
        assert from_maestro == ["speedup-04-maestro-retry"]
        assert len(SPECS) - len(from_maestro) == 5

    def test_the_default_turing_repo_resolves_to_this_checkout(self):
        assert (DEFAULT_TURING_REPO / "pyproject.toml").is_file()
        assert (DEFAULT_TURING_REPO / "src" / "turing").is_dir()

    def test_the_roots_are_parameters_so_the_corpus_is_relocatable(self, tmp_path):
        specs = speedup_specs(turing_repo=tmp_path / "t", maestro_repo=tmp_path / "m")
        assert specs[0].source_root == tmp_path / "t"
        assert specs[3].source_root == tmp_path / "m"

    def test_maestro_keeps_node_modules_because_vitest_runs_out_of_it(self):
        assert "node_modules" not in SPECS["speedup-04-maestro-retry"].workspace_excludes

    def test_turing_problems_skip_node_modules(self):
        assert "node_modules" in SPECS["speedup-01-claim-scorer"].workspace_excludes

    def test_turing_workspaces_do_not_copy_the_eval_set(self):
        """Confirmed 2026-08-12: catalog, brief, floors, split and loopholes were copied."""
        secrets = {"research", "test_research", "docs"}
        for spec in SPECS.values():
            if spec.source_root == DEFAULT_MAESTRO_REPO:
                assert secrets.isdisjoint(spec.workspace_excludes), spec.id
                continue
            assert secrets <= set(spec.workspace_excludes), spec.id

    def test_no_problem_copies_its_git_directory(self):
        for spec in SPECS.values():
            assert ".git" in spec.workspace_excludes, spec.id


class TestTargetsExist:
    """The declared targets must be real files in the repos as they stand.

    A corpus pointing at a path that does not exist would score six failures
    for reasons unrelated to the idea being tested — the exact shape of the
    round-0-near-zero kill condition firing on an artefact.
    """

    @pytest.mark.parametrize(
        "problem_id",
        [
            "speedup-01-claim-scorer",
            "speedup-02-vault-index-query",
            "speedup-03-hash-embedder",
            "speedup-05-embedding-model-config",
            "speedup-06-replay-window-evict",
        ],
    )
    def test_the_turing_target_file_exists(self, problem_id):
        relative = SPECS[problem_id].target.split(":")[0]
        assert (DEFAULT_TURING_REPO / relative).is_file()

    def test_the_corpus_is_a_pure_function_of_its_arguments(self):
        first = speedup_specs()
        second = speedup_specs()
        assert [s.fingerprint_material() for s in first] == [
            s.fingerprint_material() for s in second
        ]


class TestHarnessBoundary:
    """Benchmark drivers and report commands must not live in the write surface.

    Confirmed 2026-08-12: speedup-04's `npm test` ran from the workspace, so a
    one-line package.json rewrite produced a 3370× "speedup" with a passing
    gate. The `{harness}` placeholder is the ownership marker; missing_harness
    scripts only scans elements that carry it, so a workspace-owned driver was
    also invisible to the pre-round check.
    """

    def test_every_timing_driver_lives_under_harness(self):
        for spec in SPECS.values():
            assert argv_invokes_harness(spec.timing.argv), spec.id

    def test_no_catalog_command_is_a_workspace_owned_package_script(self):
        for spec in SPECS.values():
            assert not argv_is_workspace_package_script(spec.timing.argv), spec.id
            for command in spec.gate.commands:
                assert not argv_is_workspace_package_script(command.argv), (
                    spec.id,
                    command.argv,
                )

    def test_maestro_timing_and_vitest_report_are_harness_drivers(self):
        spec = SPECS["speedup-04-maestro-retry"]
        assert spec.timing.argv == (
            "{python}",
            "{harness}/benchmarks/speedup_04_maestro_retry.py",
            "--workspace",
            "{workspace}",
        )
        report = spec.gate.commands[0]
        assert report.argv == (
            "{python}",
            "{harness}/artifacts/speedup_04_run_vitest.py",
            "--workspace",
            "{workspace}",
            "--out",
            "vitest-report.json",
        )
        assert report.expect_success is False

    def test_maestro_missing_drivers_are_visible_to_the_pre_round_check(self, tmp_path):
        from turing.research.problems.speedup import SpeedupAdapter

        adapter = SpeedupAdapter(
            harness_root=tmp_path / "harness",
            reference_root=tmp_path / "reference",
            specs=(SPECS["speedup-04-maestro-retry"],),
        )
        missing = {path.name for path in adapter.missing_harness_scripts()}
        assert "speedup_04_maestro_retry.py" in missing
        assert "speedup_04_run_vitest.py" in missing
