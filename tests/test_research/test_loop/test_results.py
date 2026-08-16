"""Tests for the results-reporting seam — ``turing.research.loop.results``.

Follows the style of ``test_workspace.py``: pytest classes grouping related
behaviour, one behaviour per test, async tests relying on ``asyncio_mode =
"auto"`` (no ``@pytest.mark.asyncio`` needed).

A few tests here exist specifically to pin the validation *asymmetry* the
module is built around — ``metrics`` raises, ``diagnostics`` never does — and
to pin that the four cap fields (``total_steps``/``tokens_cap``/``steps_cap``/
``wall_clock_cap_s``) are never omitted, matching ``Cap.__post_init__``'s
guarantee that none of those dimensions is ever ``None`` or non-positive. See
the implementer's final report for the spec passage this resolves a
contradiction against.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import pytest
import structlog

from turing.research.contracts import AttemptState, ContractViolationError
from turing.research.loop import results
from turing.research.loop.integrity import verify_metrics_chain
from turing.research.loop.results import (
    AGENT_DIAGNOSTICS_RELPATH,
    RESERVED_FIELDS,
    MetricsLine,
    MetricsWriter,
    Outcome,
    ProgressTracker,
    read_agent_diagnostics,
    read_metrics_points,
    usable_target,
    write_attempt_summary,
    write_round_summary,
    write_viewer_config,
)

if TYPE_CHECKING:
    from pathlib import Path

# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #


def _header(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "attempt_id": "attempt-1",
        "problem_id": "problem-1",
        "round_id": "round-00",
        "seed": 7,
        "score_scale": "speedup",
        "started_at_ms": 1_700_000_000_000,
    }
    base.update(overrides)
    return base


def _line(**overrides: object) -> MetricsLine:
    base: dict[str, Any] = dict(
        step=1,
        total_steps=50,
        ts=1_700_000_000.0,
        outcome=Outcome.RUNNING,
        correctness_pass=None,
        tokens_used=100,
        tokens_cap=1_000_000,
        steps_cap=50,
        consumed_steps=1,
        wall_clock_s=10.0,
        wall_clock_cap_s=10_800.0,
        cap_extensions=0,
        step_wall_clock_s=5.0,
        verify_wall_clock_s=2.0,
        step_tokens=100,
        made_progress=None,
        progress=None,
        metrics={},
        diagnostics={},
    )
    base.update(overrides)
    return MetricsLine(**base)


def _attempt_summary_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        problem_id="p1",
        attempt_id="a1",
        round_id="round-00",
        seed=7,
        problem_type="speedup",
        split="practice",
        score_scale="speedup",
        outcome=Outcome.SOLVED,
        final_state="passed",
        best_score=1.5,
        best_passed_correctness=True,
        baseline_score=1.0,
        target_score=2.0,
        final_progress=0.5,
        consumed_steps=5,
        consumed_tokens=1000,
        consumed_wall_clock_seconds=60.0,
        cap_extensions=0,
        escalation_count=0,
        steps_recorded=5,
    )
    base.update(overrides)
    return base


def _round_summary_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        round_index=0,
        run_id="run-1",
        parent_round_id=None,
        eval_set_hash="hash123",
        seed=7,
        comparable_to_parent=True,
        cells=[
            {
                "cell": "speedup/practice",
                "problem_type": "speedup",
                "split": "practice",
                "mean_score": 1.5,
                "n": 3,
                "correctness_passes": 3,
            },
            {
                "cell": "kaggle/practice",
                "problem_type": "kaggle",
                "split": "practice",
                "mean_score": 0.8,
                "n": 2,
                "correctness_passes": 1,
            },
        ],
        wall_clock_seconds=100.0,
        tokens=5000,
        attempts=5,
        escalations=1,
        verdict="continue",
    )
    base.update(overrides)
    return base


def _write_diagnostics(workspace: Path, text: str) -> None:
    target = workspace / AGENT_DIAGNOSTICS_RELPATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# RESERVED_FIELDS
# --------------------------------------------------------------------------- #


class TestReservedFields:
    def test_it_matches_the_desktop_panes_excluded_series_keys(self) -> None:
        """webui/src/desktop/panes/metrics.ts EXCLUDED_SERIES_KEYS, mirrored by hand."""
        assert frozenset({"step", "total_steps", "ts"}) == RESERVED_FIELDS


# --------------------------------------------------------------------------- #
# Outcome
# --------------------------------------------------------------------------- #


class TestOutcomeFromAttemptState:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (AttemptState.PENDING, Outcome.RUNNING),
            (AttemptState.RUNNING, Outcome.RUNNING),
            (AttemptState.VERIFYING, Outcome.RUNNING),
            (AttemptState.PASSED, Outcome.SOLVED),
            (AttemptState.FAILED_WITHIN_CAP, Outcome.FAILED_WITHIN_CAP),
            (AttemptState.ESCALATED, Outcome.ESCALATED),
            (AttemptState.ABANDONED, Outcome.ABANDONED),
            (AttemptState.PAUSED, Outcome.PAUSED),
        ],
    )
    def test_maps_every_attempt_state_member(self, state: AttemptState, expected: Outcome) -> None:
        assert Outcome.from_attempt_state(state) is expected

    def test_the_mapping_is_exhaustive_over_every_attempt_state_member(self) -> None:
        """A future AttemptState member must fail loudly, not chart as RUNNING."""
        assert set(AttemptState) == set(results._ATTEMPT_STATE_TO_OUTCOME)

    def test_an_unmapped_state_raises_naming_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delitem(results._ATTEMPT_STATE_TO_OUTCOME, AttemptState.PAUSED)
        with pytest.raises(ContractViolationError, match="PAUSED"):
            Outcome.from_attempt_state(AttemptState.PAUSED)

    def test_paused_has_its_own_code_distinct_from_running(self) -> None:
        """working != waiting to be resumed."""
        assert int(Outcome.PAUSED) == 5
        assert Outcome.PAUSED is not Outcome.RUNNING


# --------------------------------------------------------------------------- #
# ProgressTracker
# --------------------------------------------------------------------------- #


class TestProgressTracker:
    def test_no_target_returns_none_for_every_observation(self) -> None:
        tracker = ProgressTracker(target=None)
        assert tracker.observe(0.0) is None
        assert tracker.observe(999.0) is None

    def test_no_target_still_records_the_first_score_as_baseline(self) -> None:
        """The baseline is a fact about the run, independent of whether there
        is a target to measure progress against. Reconcile derives
        ``baseline_score`` from this even on problems with no PassCriterion.
        """
        tracker = ProgressTracker(target=None)
        assert tracker.baseline is None
        assert tracker.observe(3.0) is None
        assert tracker.baseline == 3.0
        for score in (4.0, 5.0, 999.0):
            assert tracker.observe(score) is None
        assert tracker.baseline == 3.0

    def test_first_observation_sets_the_baseline_and_returns_zero(self) -> None:
        tracker = ProgressTracker(target=10.0)
        assert tracker.observe(2.0) == 0.0
        assert tracker.baseline == 2.0

    def test_reaching_the_target_returns_one(self) -> None:
        tracker = ProgressTracker(target=10.0)
        tracker.observe(0.0)
        assert tracker.observe(10.0) == 1.0

    def test_a_score_between_baseline_and_target_is_the_linear_fraction(self) -> None:
        tracker = ProgressTracker(target=10.0)
        tracker.observe(0.0)
        assert tracker.observe(5.0) == pytest.approx(0.5)

    def test_clamps_above_the_target_to_one(self) -> None:
        tracker = ProgressTracker(target=10.0)
        tracker.observe(0.0)
        assert tracker.observe(50.0) == 1.0

    def test_clamps_below_the_baseline_to_zero(self) -> None:
        tracker = ProgressTracker(target=10.0)
        tracker.observe(5.0)
        assert tracker.observe(-100.0) == 0.0

    def test_target_at_or_below_baseline_returns_none_and_warns_exactly_once(self) -> None:
        """A 500-step attempt must not log this 500 times."""
        tracker = ProgressTracker(target=1.0)
        with structlog.testing.capture_logs() as cap:
            for score in (5.0, 6.0, 7.0, 8.0, 9.0):
                assert tracker.observe(score) is None
        warns = [e for e in cap if e.get("event") == "research.results.degenerate_target"]
        assert len(warns) == 1
        assert warns[0]["log_level"] == "warning"
        assert warns[0]["target"] == 1.0
        assert warns[0]["baseline"] == 5.0

    def test_target_exactly_equal_to_baseline_also_refuses(self) -> None:
        """target <= baseline, not target < baseline."""
        tracker = ProgressTracker(target=5.0)
        with structlog.testing.capture_logs() as cap:
            assert tracker.observe(5.0) is None
        assert [e for e in cap if e.get("event") == "research.results.degenerate_target"]

    @pytest.mark.parametrize("bad_target", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_target_refuses_every_observation_instead_of_fabricating_one(
        self, bad_target: float
    ) -> None:
        """Every comparison against NaN is False, so ``target <= baseline``
        alone never catches ``target=nan`` and falls through to
        ``max(0.0, min(1.0, nan))``, which resolves to ``1.0`` — a fabricated
        "solved" reading for an attempt whose own outcome says it failed.
        +inf and -inf are equally degenerate: there is no meaningful fraction
        of the way to an unreachable or undefined target.
        """
        tracker = ProgressTracker(target=bad_target)
        with structlog.testing.capture_logs() as cap:
            for score in (1.0, 2.0, 3.0):
                assert tracker.observe(score) is None
        warns = [e for e in cap if e.get("event") == "research.results.degenerate_target"]
        assert len(warns) == 1
        assert warns[0]["log_level"] == "warning"
        # target is captured in the event even though it is not finite.
        assert "target" in warns[0]

    def test_a_non_finite_target_never_reaches_1_0(self) -> None:
        """Pinning the exact fabrication bug: NaN-blind max/min resolving to
        1.0 in this argument order, not some other wrong value.
        """
        tracker = ProgressTracker(target=float("nan"))
        result = tracker.observe(5.0)
        assert result is None
        assert result != 1.0


# --------------------------------------------------------------------------- #
# MetricsLine.to_json — emission order and omission rules
# --------------------------------------------------------------------------- #


class TestMetricsLineEmission:
    def test_step_is_the_first_key_and_outcome_code_is_an_int(self) -> None:
        payload = _line(outcome=Outcome.SOLVED).to_json()
        assert next(iter(payload)) == "step"
        assert payload["outcome_code"] == 1
        assert isinstance(payload["outcome_code"], int)
        assert not isinstance(payload["outcome_code"], bool)

    def test_correctness_pass_made_progress_and_progress_are_omitted_when_none(self) -> None:
        payload = _line(correctness_pass=None, made_progress=None, progress=None).to_json()
        assert "correctness_pass" not in payload
        assert "made_progress" not in payload
        assert "progress" not in payload

    def test_correctness_pass_made_progress_and_progress_are_present_when_set(self) -> None:
        payload = _line(correctness_pass=True, made_progress=False, progress=0.5).to_json()
        assert payload["correctness_pass"] == 1
        assert payload["made_progress"] == 0
        assert payload["progress"] == 0.5

    def test_correctness_pass_serialises_as_one_zero_not_true_false(self) -> None:
        payload = _line(correctness_pass=True).to_json()
        assert payload["correctness_pass"] == 1
        assert payload["correctness_pass"] is not True

    def test_cap_fields_are_never_omitted_even_though_they_could_be_falsy_looking(self) -> None:
        """Cap.__post_init__ rejects any non-positive dimension: there is no
        ``None`` state for total_steps/tokens_cap/steps_cap/wall_clock_cap_s to
        be in, so to_json must never write an "omit when unset" branch for them.
        """
        payload = _line(total_steps=1, tokens_cap=1, steps_cap=1, wall_clock_cap_s=0.001).to_json()
        assert payload["total_steps"] == 1
        assert payload["tokens_cap"] == 1
        assert payload["steps_cap"] == 1
        assert payload["wall_clock_cap_s"] == 0.001

    def test_cap_extensions_step_tokens_and_wall_clock_fields_are_always_present(self) -> None:
        payload = _line().to_json()
        for key in (
            "tokens_used",
            "tokens_cap",
            "steps_cap",
            "consumed_steps",
            "wall_clock_s",
            "wall_clock_cap_s",
            "cap_extensions",
            "step_wall_clock_s",
            "verify_wall_clock_s",
            "step_tokens",
        ):
            assert key in payload

    def test_consumed_steps_is_emitted_immediately_after_steps_cap(self) -> None:
        """consumed_steps is cap accounting, distinct from step (the solver's
        step index / chart x-axis) — they coincide only on the happy path, so
        reconcile must compare consumed_steps directly rather than deriving it
        from step. It rides on every line, immediately after steps_cap.
        """
        payload = _line(consumed_steps=3).to_json()
        keys = list(payload.keys())
        assert keys.index("consumed_steps") == keys.index("steps_cap") + 1

    def test_consumed_steps_can_legitimately_differ_from_step(self) -> None:
        """_charge_failed_step bumps consumed.steps without bumping step_index:
        a proposal call that happened still costs a step even when parse/apply
        then failed. step=0 alongside consumed_steps=2 is honest data.
        """
        payload = _line(step=0, consumed_steps=2).to_json()
        assert payload["step"] == 0
        assert payload["consumed_steps"] == 2

    def test_metrics_entries_land_in_the_payload(self) -> None:
        payload = _line(metrics={"speedup": 1.5}).to_json()
        assert payload["speedup"] == 1.5

    def test_diagnostics_entries_are_prefixed_diag_and_land_last(self) -> None:
        payload = _line(metrics={"speedup": 1.5}, diagnostics={"peak_rss_mb": 400.0}).to_json()
        assert payload["diag_peak_rss_mb"] == 400.0
        keys = list(payload.keys())
        assert keys.index("diag_peak_rss_mb") > keys.index("speedup")

    def test_a_diagnostics_key_already_prefixed_is_not_double_prefixed(self) -> None:
        payload = _line(diagnostics={"diag_already": 2.0}).to_json()
        assert payload["diag_already"] == 2.0
        assert "diag_diag_already" not in payload


# --------------------------------------------------------------------------- #
# MetricsLine.to_json — the strict "metrics" ruler
# --------------------------------------------------------------------------- #


class TestScoredMetricsValidation:
    @pytest.mark.parametrize("key", ["step", "total_steps", "ts"])
    def test_a_reserved_field_key_raises_naming_it(self, key: str) -> None:
        with pytest.raises(ContractViolationError, match=key):
            _line(metrics={key: 1.0}).to_json()

    @pytest.mark.parametrize("key", ["progress", "tokens_used", "outcome_code", "consumed_steps"])
    def test_a_core_key_collision_raises_naming_it(self, key: str) -> None:
        with pytest.raises(ContractViolationError, match=key):
            _line(metrics={key: 1.0}).to_json()

    def test_an_empty_string_key_raises(self) -> None:
        with pytest.raises(ContractViolationError):
            _line(metrics={"": 1.0}).to_json()

    def test_a_diag_prefixed_key_raises(self) -> None:
        with pytest.raises(ContractViolationError, match="diag_"):
            _line(metrics={"diag_speedup": 1.0}).to_json()

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, "x"])
    def test_a_bad_value_raises(self, value: object) -> None:
        with pytest.raises(ContractViolationError):
            _line(metrics={"speedup": value}).to_json()

    def test_a_problem_declaring_score_scale_progress_fails_at_the_first_line(self) -> None:
        """The runner names the metrics key after problem.verifier.score_scale,
        which is free-form; a scale of "progress" must not clobber the axis.
        """
        with pytest.raises(ContractViolationError):
            _line(metrics={"progress": 0.5}).to_json()

    def test_non_finite_ts_raises(self) -> None:
        with pytest.raises(ContractViolationError, match="ts"):
            _line(ts=float("nan")).to_json()

    def test_non_finite_wall_clock_s_raises(self) -> None:
        with pytest.raises(ContractViolationError, match="wall_clock_s"):
            _line(wall_clock_s=float("inf")).to_json()

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_progress_raises(self, bad: float) -> None:
        """progress was previously unguarded: MetricsPane.tsx's parser
        silently drops any line JSON.parse rejects, so a non-finite progress
        used to delete the whole step from the chart with no error anywhere.
        """
        with pytest.raises(ContractViolationError, match="progress"):
            _line(progress=bad).to_json()

    def test_progress_none_is_still_omitted_not_raised(self) -> None:
        """None is the legitimate "no target" / "degenerate target" state,
        not a non-finite value — it must still be omitted, not raise.
        """
        payload = _line(progress=None).to_json()
        assert "progress" not in payload

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_step_wall_clock_s_raises(self, bad: float) -> None:
        with pytest.raises(ContractViolationError, match="step_wall_clock_s"):
            _line(step_wall_clock_s=bad).to_json()

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_verify_wall_clock_s_raises(self, bad: float) -> None:
        with pytest.raises(ContractViolationError, match="verify_wall_clock_s"):
            _line(verify_wall_clock_s=bad).to_json()

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_step_tokens_raises(self, bad: float) -> None:
        with pytest.raises(ContractViolationError, match="step_tokens"):
            _line(step_tokens=bad).to_json()

    def test_a_non_finite_value_in_any_of_the_four_fields_never_produces_a_line(
        self,
    ) -> None:
        """None of these four should ever reach json.dumps and produce an
        unparseable JSONL line — they must raise before serialisation.
        """
        for kwargs in (
            {"progress": float("nan")},
            {"step_wall_clock_s": float("inf")},
            {"verify_wall_clock_s": float("-inf")},
            {"step_tokens": float("nan")},
        ):
            with pytest.raises(ContractViolationError):
                _line(**kwargs).to_json()


# --------------------------------------------------------------------------- #
# MetricsLine.to_json — the lenient "diagnostics" notebook
# --------------------------------------------------------------------------- #


class TestDiagnosticsValidation:
    def test_bad_diagnostics_entries_are_dropped_not_raised(self) -> None:
        payload = _line(
            diagnostics={
                "good": 1.0,
                "bad_nan": float("nan"),
                "bad_inf": float("inf"),
                "bad_bool": True,
                "bad_str": "x",
            }
        ).to_json()
        assert payload["diag_good"] == 1.0
        for bad in ("diag_bad_nan", "diag_bad_inf", "diag_bad_bool", "diag_bad_str"):
            assert bad not in payload

    def test_an_empty_diagnostics_key_is_dropped_not_raised(self) -> None:
        payload = _line(diagnostics={"": 1.0, "good": 2.0}).to_json()
        assert payload["diag_good"] == 2.0
        assert "diag_" not in payload


# --------------------------------------------------------------------------- #
# MetricsWriter
# --------------------------------------------------------------------------- #


class TestMetricsWriter:
    async def test_it_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dirs" / "metrics.jsonl"
        writer = MetricsWriter(path, header=_header())
        await writer.append(_line())
        assert path.exists()

    async def test_appends_rather_than_truncating(self, tmp_path: Path) -> None:
        path = tmp_path / "metrics.jsonl"
        writer = MetricsWriter(path, header=_header())
        await writer.append(_line(step=1))
        first_write = path.read_text()
        await writer.append(_line(step=2))
        second_write = path.read_text()
        assert second_write.startswith(first_write)
        assert len(second_write.splitlines()) == 2

    async def test_each_append_writes_exactly_one_newline_terminated_line(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "metrics.jsonl"
        writer = MetricsWriter(path, header=_header())
        await writer.append(_line())
        text = path.read_text()
        assert text.endswith("\n")
        assert text.count("\n") == 1

    async def test_line_count_increments_only_on_a_successful_append(self, tmp_path: Path) -> None:
        path = tmp_path / "metrics.jsonl"
        writer = MetricsWriter(path, header=_header())
        assert writer.line_count == 0
        await writer.append(_line(step=1))
        assert writer.line_count == 1
        with pytest.raises(ContractViolationError):
            await writer.append(_line(step=2, metrics={"step": 1.0}))
        assert writer.line_count == 1

    async def test_a_failed_append_does_not_write_a_partial_line(self, tmp_path: Path) -> None:
        path = tmp_path / "metrics.jsonl"
        writer = MetricsWriter(path, header=_header())
        await writer.append(_line(step=1))
        before = path.read_text()
        with pytest.raises(ContractViolationError):
            await writer.append(_line(step=2, metrics={"total_steps": 1.0}))
        assert path.read_text() == before

    async def test_every_line_carries_a_chain_string_and_the_chain_verifies(
        self, tmp_path: Path
    ) -> None:
        writer = MetricsWriter(tmp_path / "metrics.jsonl", header=_header())
        for i in range(3):
            await writer.append(_line(step=i))
        lines = (tmp_path / "metrics.jsonl").read_text().splitlines()
        for index, raw_line in enumerate(lines):
            payload = json.loads(raw_line)
            assert isinstance(payload["_chain"], str)
            assert payload["_chain"].startswith(f"{index}:")
        verdict = await verify_metrics_chain(tmp_path)
        assert verdict.ok is True
        assert verdict.lines_checked == 3

    async def test_chain_head_and_line_count_advance_together(self, tmp_path: Path) -> None:
        writer = MetricsWriter(tmp_path / "metrics.jsonl", header=_header())
        seed = writer.chain_head
        await writer.append(_line(step=0))
        assert writer.chain_head != seed
        assert writer.line_count == 1


# --------------------------------------------------------------------------- #
# MetricsWriter — refuses to splice onto an existing chain
# --------------------------------------------------------------------------- #


class TestMetricsWriterRefusesToSpliceOntoAnExistingChain:
    """Regression coverage for the two-honest-attempts-in-one-file bug.

    Two honest calls to ``run_attempt`` with the same ``output_dir`` used to
    leave ``metrics.jsonl`` with lines chained from two different seeds — a
    FAIL byte-for-byte indistinguishable from real tampering, and (since the
    desktop pane tails every parseable line with no knowledge of ``_chain``)
    one continuous chart drawn out of two separate attempts.
    """

    async def test_a_non_empty_existing_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "metrics.jsonl"
        first = MetricsWriter(path, header=_header(attempt_id="attempt-1"))
        await first.append(_line(step=0))
        assert path.stat().st_size > 0

        with pytest.raises(ContractViolationError):
            MetricsWriter(path, header=_header(attempt_id="attempt-2"))

    async def test_the_existing_file_is_left_untouched_by_the_refusal(self, tmp_path: Path) -> None:
        path = tmp_path / "metrics.jsonl"
        first = MetricsWriter(path, header=_header(attempt_id="attempt-1"))
        await first.append(_line(step=0))
        before = path.read_text()

        with pytest.raises(ContractViolationError):
            MetricsWriter(path, header=_header(attempt_id="attempt-2"))

        assert path.read_text() == before

    async def test_a_non_empty_sidecar_with_no_log_still_raises(self, tmp_path: Path) -> None:
        """RES-19: a half-rotated ``prior-N/`` looks like this from the inside.

        ``runner._rotate_stale_metrics`` now moves the trio as one atomic
        set, but the residual window between "some files moved" and "the
        rotation committed" can still leave a real ``metrics.chain.json``
        sitting with no ``metrics.jsonl`` beside it -- exactly the shape a
        crash mid-rotation used to (and, in the residual window, still can)
        produce. A writer that only checked ``metrics.jsonl`` would read
        "missing" here and start appending as if this were a fresh
        directory, silently discarding the evidence that a rotation was left
        half-done. The refusal must fire on the sidecar alone.
        """
        path = tmp_path / "metrics.jsonl"
        sidecar_path = tmp_path / "metrics.chain.json"
        first = MetricsWriter(path, header=_header(attempt_id="attempt-1"))
        await first.append(_line(step=0))
        assert sidecar_path.stat().st_size > 0
        path.unlink()
        assert not path.exists()

        with pytest.raises(ContractViolationError):
            MetricsWriter(path, header=_header(attempt_id="attempt-2"))

    def test_a_missing_file_does_not_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "does" / "not" / "exist" / "metrics.jsonl"
        MetricsWriter(path, header=_header())  # must not raise

    def test_an_empty_existing_file_does_not_raise(self, tmp_path: Path) -> None:
        """A zero-byte file is not a chain — an attempt directory can
        legitimately be pre-created (e.g. by mkdir) without a prior attempt
        ever having appended to it.
        """
        path = tmp_path / "metrics.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        assert path.stat().st_size == 0

        MetricsWriter(path, header=_header())  # must not raise

    async def test_a_fresh_writer_over_a_missing_file_still_works_end_to_end(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "metrics.jsonl"
        writer = MetricsWriter(path, header=_header())
        await writer.append(_line(step=0))
        assert writer.line_count == 1
        assert path.exists()


# --------------------------------------------------------------------------- #
# read_agent_diagnostics
# --------------------------------------------------------------------------- #


class TestReadAgentDiagnostics:
    async def test_a_missing_file_returns_empty_dict_and_logs_at_debug(
        self, tmp_path: Path
    ) -> None:
        with structlog.testing.capture_logs() as cap:
            result = await read_agent_diagnostics(tmp_path)
        assert result == {}
        debugs = [e for e in cap if e.get("event") == "research.results.diagnostics_absent"]
        assert len(debugs) == 1
        assert debugs[0]["log_level"] == "debug"
        assert not [e for e in cap if e.get("log_level") == "warning"]

    @pytest.mark.skipif(
        hasattr(os, "getuid") and os.getuid() == 0,
        reason="root bypasses file permissions, so this cannot force an unreadable file",
    )
    async def test_an_unreadable_file_returns_empty_dict(self, tmp_path: Path) -> None:
        target = tmp_path / AGENT_DIAGNOSTICS_RELPATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"x": 1.0}', encoding="utf-8")
        target.chmod(0o000)
        try:
            result = await read_agent_diagnostics(tmp_path)
        finally:
            target.chmod(0o644)
        assert result == {}

    async def test_malformed_json_returns_empty_dict(self, tmp_path: Path) -> None:
        _write_diagnostics(tmp_path, "{not valid json")
        assert await read_agent_diagnostics(tmp_path) == {}

    async def test_a_json_array_instead_of_an_object_returns_empty_dict(
        self, tmp_path: Path
    ) -> None:
        _write_diagnostics(tmp_path, "[1, 2, 3]")
        assert await read_agent_diagnostics(tmp_path) == {}

    async def test_an_oversized_file_is_rejected_unparsed_and_logs_once(
        self, tmp_path: Path
    ) -> None:
        huge = json.dumps({f"k{i}": float(i) for i in range(20_000)})
        assert len(huge.encode("utf-8")) > 256 * 1024
        _write_diagnostics(tmp_path, huge)
        with structlog.testing.capture_logs() as cap:
            result = await read_agent_diagnostics(tmp_path)
        assert result == {}
        warns = [e for e in cap if e.get("event") == "research.results.diagnostics_too_large"]
        assert len(warns) == 1
        assert warns[0]["log_level"] == "warning"

    async def test_200_keys_truncates_to_50_in_sorted_order_and_logs_once(
        self, tmp_path: Path
    ) -> None:
        data = {f"k{i:03d}": float(i) for i in range(200)}
        _write_diagnostics(tmp_path, json.dumps(data))
        with structlog.testing.capture_logs() as cap:
            result = await read_agent_diagnostics(tmp_path)
        assert len(result) == 50
        assert list(result.keys()) == sorted(data.keys())[:50]
        warns = [e for e in cap if e.get("event") == "research.results.diagnostics_truncated"]
        assert len(warns) == 1
        assert warns[0]["log_level"] == "warning"

    async def test_nested_objects_are_dropped(self, tmp_path: Path) -> None:
        _write_diagnostics(tmp_path, json.dumps({"good": 1.0, "nested": {"a": 1}}))
        assert await read_agent_diagnostics(tmp_path) == {"good": 1.0}

    async def test_string_values_are_dropped(self, tmp_path: Path) -> None:
        _write_diagnostics(tmp_path, json.dumps({"good": 1.0, "bad": "x"}))
        assert await read_agent_diagnostics(tmp_path) == {"good": 1.0}

    async def test_bool_values_are_dropped(self, tmp_path: Path) -> None:
        _write_diagnostics(tmp_path, json.dumps({"good": 1.0, "bad": True}))
        assert await read_agent_diagnostics(tmp_path) == {"good": 1.0}

    async def test_nan_values_are_dropped(self, tmp_path: Path) -> None:
        _write_diagnostics(tmp_path, '{"good": 1.0, "bad": NaN}')
        assert await read_agent_diagnostics(tmp_path) == {"good": 1.0}

    async def test_keys_returned_unprefixed(self, tmp_path: Path) -> None:
        _write_diagnostics(tmp_path, json.dumps({"peak_rss_mb": 400.0}))
        result = await read_agent_diagnostics(tmp_path)
        assert result == {"peak_rss_mb": 400.0}


# --------------------------------------------------------------------------- #
# write_attempt_summary / write_round_summary
# --------------------------------------------------------------------------- #


class TestWriteAttemptSummary:
    async def test_it_writes_a_json_object_whose_first_key_is_schema_version(
        self, tmp_path: Path
    ) -> None:
        path = await write_attempt_summary(tmp_path, **_attempt_summary_kwargs())
        assert path == tmp_path / "metrics.json"
        text = path.read_text()
        payload = json.loads(text)
        assert isinstance(payload, dict)
        assert next(iter(payload)) == "schema_version"
        assert payload["schema_version"] == 1

    async def test_outcome_is_written_as_a_plain_int(self, tmp_path: Path) -> None:
        path = await write_attempt_summary(
            tmp_path, **_attempt_summary_kwargs(outcome=Outcome.ESCALATED)
        )
        payload = json.loads(path.read_text())
        assert payload["outcome"] == 3
        assert isinstance(payload["outcome"], int)

    async def test_it_creates_parent_directories(self, tmp_path: Path) -> None:
        directory = tmp_path / "attempts" / "p1"
        path = await write_attempt_summary(directory, **_attempt_summary_kwargs())
        assert path.exists()

    async def test_a_non_finite_field_raises_instead_of_writing_a_bare_nan_token(
        self, tmp_path: Path
    ) -> None:
        """json.loads happily accepts a bare NaN token back, so reconcile
        reading this file with the same library would report a clean chain
        over a file every other JSON reader — including the desktop —
        rejects outright. allow_nan=False must fail at the write instead.

        Driven through ``baseline_score``, deliberately not ``target_score``.
        ``target_score`` is the one field on this summary with a defined
        normalisation (see :class:`TestNonFiniteTargetScore`), because
        ``PassCriterion.min_score`` is the one number reaching here that
        nothing upstream finiteness-checks — ``VerificationResult.score``,
        which is where ``baseline_score`` and ``best_score`` come from, is
        guarded in ``contracts.py``. A non-finite value in any *other* field
        therefore means a guard upstream broke, and must stay loud.
        """
        with pytest.raises(ValueError):
            await write_attempt_summary(
                tmp_path, **_attempt_summary_kwargs(baseline_score=float("nan"))
            )
        assert (
            not (tmp_path / "metrics.json").exists()
            or "NaN" not in (tmp_path / "metrics.json").read_text()
        )

    async def test_no_bare_nan_or_infinity_token_ever_lands_in_metrics_json(
        self, tmp_path: Path
    ) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                await write_attempt_summary(tmp_path, **_attempt_summary_kwargs(best_score=bad))
            metrics_json = tmp_path / "metrics.json"
            if metrics_json.exists():
                text = metrics_json.read_text()
                assert "NaN" not in text
                assert "Infinity" not in text


class TestUsableTarget:
    """``usable_target`` — the module's one answer to "is this a target?"."""

    def test_a_finite_target_passes_through_unchanged(self) -> None:
        assert usable_target(2.0) == 2.0
        assert usable_target(0.0) == 0.0
        assert usable_target(-3.5) == -3.5

    def test_none_stays_none(self) -> None:
        """No criterion and an unusable criterion collapse to the same value
        on purpose: neither one is a bar the run can be measured against.
        """
        assert usable_target(None) is None

    @pytest.mark.parametrize("bad_target", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_target_is_not_a_target(self, bad_target: float) -> None:
        assert usable_target(bad_target) is None

    def test_it_agrees_with_progress_tracker_on_what_it_refuses(self) -> None:
        """The two must not drift: ``ProgressTracker.observe`` refuses to
        normalise against exactly the targets this function calls unusable,
        and the summary's ``target_score`` is written from this one while
        ``final_progress`` is written from the tracker's. If they disagreed,
        ``metrics.json`` would record a bar next to a null progress reading
        that was never measured against it.
        """
        for bad_target in (float("nan"), float("inf"), float("-inf")):
            assert usable_target(bad_target) is None
            assert ProgressTracker(target=bad_target).observe(1.0) is None


class TestNonFiniteTargetScore:
    """The round-5 false alarm, at the writer.

    ``PassCriterion.min_score`` is not finiteness-checked at construction, so
    a ``nan``/``+inf``/``-inf`` target reaches this function raw. Written
    through, it made ``json.dumps(..., allow_nan=False)`` raise; ``runner.py``
    swallowed that as ``research.results.emit_failed`` exactly as its
    sanctioned "reporting must not kill a research run" guard is designed to;
    and an entirely honest attempt ended up with **no ``metrics.json`` at
    all**, which ``verify`` then reported as ``metrics.json is missing`` with
    exit ``1``. The fix is at the emitter, never at the check.
    """

    @pytest.mark.parametrize("bad_target", [float("nan"), float("inf"), float("-inf")])
    async def test_a_non_finite_target_score_is_written_as_null_not_raised_on(
        self, tmp_path: Path, bad_target: float
    ) -> None:
        path = await write_attempt_summary(
            tmp_path, **_attempt_summary_kwargs(target_score=bad_target, final_progress=None)
        )
        payload = json.loads(path.read_text())
        assert payload["target_score"] is None
        text = path.read_text()
        assert "NaN" not in text
        assert "Infinity" not in text

    @pytest.mark.parametrize("bad_target", [float("nan"), float("inf"), float("-inf")])
    async def test_dropping_it_logs_once_under_its_own_event_name(
        self, tmp_path: Path, bad_target: float
    ) -> None:
        """A distinct event from ``research.results.degenerate_target``, which
        ``ProgressTracker`` owns. The tracker's only fires once a score has
        been observed, so an attempt whose solver died before its first
        verification would otherwise record a garbage criterion nowhere.
        Counting one event name would not be able to tell the two apart.
        """
        with structlog.testing.capture_logs() as cap:
            await write_attempt_summary(
                tmp_path, **_attempt_summary_kwargs(target_score=bad_target)
            )
        warns = [e for e in cap if e.get("event") == "research.results.target_score_dropped"]
        assert len(warns) == 1
        assert warns[0]["log_level"] == "warning"
        assert warns[0]["problem_id"] == "p1"
        # Logged as a string: a structlog JSON renderer serialises with the
        # stdlib too, so handing it the same bare nan that made the file
        # unwritable would put an unparseable token in the log about it.
        assert isinstance(warns[0]["target_score"], str)
        assert warns[0]["target_score"] == repr(bad_target)

    async def test_a_finite_target_is_written_through_and_logs_nothing(
        self, tmp_path: Path
    ) -> None:
        with structlog.testing.capture_logs() as cap:
            path = await write_attempt_summary(
                tmp_path, **_attempt_summary_kwargs(target_score=2.0)
            )
        assert json.loads(path.read_text())["target_score"] == 2.0
        assert not [e for e in cap if e.get("event") == "research.results.target_score_dropped"]

    async def test_an_absent_target_writes_null_and_logs_nothing(self, tmp_path: Path) -> None:
        """A problem with no ``PassCriterion`` is the common case, not a
        defect — warning on it every attempt is the noise this codebase
        refuses elsewhere.
        """
        with structlog.testing.capture_logs() as cap:
            path = await write_attempt_summary(
                tmp_path, **_attempt_summary_kwargs(target_score=None, final_progress=None)
            )
        assert json.loads(path.read_text())["target_score"] is None
        assert not [e for e in cap if e.get("event") == "research.results.target_score_dropped"]


class TestWriteRoundSummary:
    async def test_it_writes_a_json_object_whose_first_key_is_schema_version(
        self, tmp_path: Path
    ) -> None:
        path = await write_round_summary(tmp_path, **_round_summary_kwargs())
        payload = json.loads(path.read_text())
        assert next(iter(payload)) == "schema_version"
        assert payload["schema_version"] == 1

    async def test_cells_pass_through_unchanged_with_no_pooled_or_total_entry(
        self, tmp_path: Path
    ) -> None:
        kwargs = _round_summary_kwargs()
        path = await write_round_summary(tmp_path, **kwargs)
        payload = json.loads(path.read_text())
        assert payload["cells"] == kwargs["cells"]
        assert len(payload["cells"]) == 2
        means = {cell["mean_score"] for cell in payload["cells"]}
        assert means == {1.5, 0.8}
        assert "mean_score" not in payload
        assert not any("pool" in key or "aggregate" in key for key in payload)


# --------------------------------------------------------------------------- #
# write_viewer_config
# --------------------------------------------------------------------------- #


class TestWriteViewerConfig:
    async def test_it_defaults_series_to_progress(self, tmp_path: Path) -> None:
        path = await write_viewer_config(tmp_path, runs=["loop-x/round-00/attempts/p1"])
        assert path == tmp_path / ".viewer.json"
        payload = json.loads(path.read_text())
        assert payload["series"] == "progress"
        assert payload["runs"] == ["loop-x/round-00/attempts/p1"]
        assert "progress" in payload["titles"]

    async def test_runs_are_written_as_posix_style_relative_strings_verbatim(
        self, tmp_path: Path
    ) -> None:
        runs = ["loop-a/round-00/attempts/p1", "loop-a/round-00/attempts/p2"]
        path = await write_viewer_config(tmp_path, runs=runs)
        payload = json.loads(path.read_text())
        assert payload["runs"] == runs

    async def test_custom_series_and_titles_override_the_defaults(self, tmp_path: Path) -> None:
        path = await write_viewer_config(
            tmp_path, runs=["a/b"], primary_series="speedup", titles={"speedup": "ratio"}
        )
        payload = json.loads(path.read_text())
        assert payload["series"] == "speedup"
        assert payload["titles"] == {"speedup": "ratio"}


# --------------------------------------------------------------------------- #
# read_metrics_points
# --------------------------------------------------------------------------- #


class TestReadMetricsPoints:
    async def test_skips_blank_and_malformed_lines_and_preserves_order(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "metrics.jsonl"
        path.write_text(
            '{"step": 1, "speedup": 1.0}\n\nnot json at all\n{"step": 2, "speedup": 1.5}\n',
            encoding="utf-8",
        )
        points = await read_metrics_points(path)
        assert len(points) == 2
        assert points[0]["step"] == 1.0
        assert points[1]["step"] == 2.0

    async def test_drops_non_numeric_values(self, tmp_path: Path) -> None:
        path = tmp_path / "metrics.jsonl"
        path.write_text(
            '{"step": 1, "note": "hi", "flag": true, "speedup": 1.0}\n', encoding="utf-8"
        )
        points = await read_metrics_points(path)
        assert points == ({"step": 1.0, "speedup": 1.0},)

    async def test_a_missing_file_returns_an_empty_tuple(self, tmp_path: Path) -> None:
        points = await read_metrics_points(tmp_path / "does-not-exist.jsonl")
        assert points == ()

    async def test_a_line_that_is_a_json_array_not_an_object_is_skipped(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "metrics.jsonl"
        path.write_text('[1, 2, 3]\n{"step": 1, "speedup": 1.0}\n', encoding="utf-8")
        points = await read_metrics_points(path)
        assert points == ({"step": 1.0, "speedup": 1.0},)
