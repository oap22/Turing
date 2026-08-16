"""Integration: ``RoundRunner`` actually writes the files the desktop reads.

``results.py`` and ``plots.py`` are unit-tested against their own contracts
elsewhere (``test_results.py``, ``test_plots.py``, ``test_integrity.py``).
This file is the other half: driving a real (fake-solver) attempt and round
through ``RoundRunner`` and asserting the reporting files actually land on
disk, in the shape the desktop's ``fsroots.rs`` walk and
``webui/src/desktop/panes/metrics.ts``/``.viewer.json`` reader expect.

Fixtures come from ``.conftest`` — the same fakes ``test_runner.py`` uses, so
the runner's own guarantees (cap enforcement, escalation, lineage) are not
re-tested here; only the reporting call sites are.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import TYPE_CHECKING

import pytest
import structlog

from turing.research.contracts import (
    AttemptState,
    Cap,
    ContractViolationError,
    EscalationReason,
    EscalationVerdict,
)
from turing.research.loop import plots as plots_module
from turing.research.loop import verify as verify_cli
from turing.research.loop.integrity import (
    CHAIN_FIELD,
    ReconcileState,
    reconcile_summary,
    verify_metrics_chain,
)
from turing.research.loop.metrics import SaturationVerdict
from turing.research.loop.protocols import SolverStep
from turing.research.loop.results import Outcome
from turing.research.loop.runner import PassCriterion

from .conftest import (
    DEFAULT_CAP,
    ExplodingSolver,
    FakeSolver,
    ScriptedEscalationChannel,
    TempWorkspaceProvider,
    make_config,
    make_problem,
    make_runner,
)
from .test_runner import floors_for

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from turing.research.contracts import Attempt, Problem
    from turing.research.loop.protocols import SolverTask
    from turing.research.loop.runner import RoundRunner
    from turing.research.loop.trajectory import TrajectoryStore

    from .conftest import FakeClock


class _TokenCostError(RuntimeError):
    """The ``_spend_carried_on_error`` shape: a backend exception that
    carries accounted tokens on ``.tokens``, the way a proposal call that
    parsed a usage-report before failing to apply would raise in production.
    """

    def __init__(self, tokens: int) -> None:
        super().__init__("proposal parsed then failed to apply")
        self.tokens = tokens


class _RaisingSolverWithAccountedTokens:
    """Raises on every step with an exception carrying ``exc.tokens`` —
    reproduces the gate's finding: ``_charge_failed_step`` bumps
    ``consumed.steps`` via ``_spend_carried_on_error`` without ever bumping
    ``attempt.step_index``, so ``step`` and ``consumed_steps`` diverge on
    perfectly honest data.
    """

    def __init__(self, tokens: int = 77) -> None:
        self._tokens = tokens
        self.calls = 0

    async def step(self, task: SolverTask, attempt: Attempt) -> object:
        self.calls += 1
        raise _TokenCostError(self._tokens)


class _CancellingSolver:
    """Raises :class:`asyncio.CancelledError` — a ``BaseException``, not an
    ``Exception``. Stands in for the round being cancelled mid-attempt, which
    ``run_attempts``' per-attempt guard must let through untouched rather than
    re-label as this problem's harness failure and carry on with the corpus.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def step(self, task: SolverTask, attempt: Attempt) -> object:
        self.calls += 1
        raise asyncio.CancelledError


def _problem_with_scale(problem_id: str, scale: str) -> Problem:
    """A problem whose verifier reports a colliding ``score_scale``.

    ``score_scale`` is free-form by design (``contracts.py``: "Free-form by
    design — each problem scores on its own scale"), and the runner names the
    metrics key holding the raw score after it. ``conftest.make_problem``
    derives the scale from the problem *type*, so it can only ever produce the
    two conventional spellings; this rebuilds the same problem around a scale
    an operator could plausibly and legitimately choose, and that
    ``results._validate_scored_metric`` then refuses.
    """
    problem = make_problem(problem_id)
    return dataclasses.replace(
        problem, verifier=dataclasses.replace(problem.verifier, score_scale=scale)
    )


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


async def _verify_exit_code(root: Path) -> int:
    """``python -m turing.research.loop.verify <root>``'s exit code.

    Run through :func:`asyncio.to_thread` rather than called directly:
    ``verify.main`` calls ``asyncio.run`` internally, and with this project's
    ``asyncio_mode = "auto"`` these tests already run inside a
    pytest-asyncio-managed loop, where nesting ``asyncio.run`` raises
    unconditionally. A worker thread has no running loop of its own, so the
    real entry point — argparse, the walk, both checks, the exit code — runs
    exactly as an operator invokes it.
    """
    return await asyncio.to_thread(verify_cli.main, [str(root)])


# --------------------------------------------------------------------------- #
# One attempt
# --------------------------------------------------------------------------- #


class TestAttemptEmission:
    async def test_a_multi_step_attempt_writes_one_metrics_line_per_step(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The Edit-C trap: attaching to the post-loop checkpoint instead of the
        in-loop one would collapse every attempt to exactly one line no matter
        how many steps it took. ``DEFAULT_CAP.max_steps == 3`` and the default
        ``FakeSolver`` never passes, so this attempt runs all three steps —
        plus the Edit-D terminal line appended once the attempt reaches its
        terminal state, for four lines total. ``steps_recorded`` is lines,
        not steps, for exactly this reason.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        problem = make_problem("s1")
        outcome = await runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0))

        metrics_path = store.round_dir(0) / "attempts" / "s1" / "metrics.jsonl"
        lines = _read_lines(metrics_path)
        assert len(lines) == DEFAULT_CAP.max_steps + 1 == 4
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP

    async def test_each_line_is_valid_json_and_carries_the_core_fields(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempt(make_problem("s1"), make_config(), output_dir=store.round_dir(0))
        lines = _read_lines(store.round_dir(0) / "attempts" / "s1" / "metrics.jsonl")
        assert lines
        for line in lines:
            for key in ("step", "ts", "outcome_code", "tokens_used", "wall_clock_s"):
                assert key in line, f"{key!r} missing from {line!r}"
            assert isinstance(line["outcome_code"], int)
            assert line[CHAIN_FIELD].split(":", 1)[0].isdigit()

    async def test_a_step_that_skips_verification_still_records_tokens_and_step(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """``DEFAULT_CAP.max_steps == 3``: the third charge trips the cap before
        verify runs (``verify_now`` requires ``not attempt.cap_exhausted``), so
        the last *in-loop* line carries no raw-score key but still carries the
        cap series. The Edit-D terminal line follows it as a fourth line,
        carrying the attempt's terminal outcome.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        problem = make_problem("s1")
        await runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0))
        lines = _read_lines(store.round_dir(0) / "attempts" / "s1" / "metrics.jsonl")
        assert len(lines) == 4
        last_in_loop = lines[-2]
        assert problem.verifier.score_scale not in last_in_loop
        assert "correctness_pass" not in last_in_loop
        assert "progress" not in last_in_loop
        assert last_in_loop["tokens_used"] == 30  # 3 steps * 10 tokens each
        assert last_in_loop["step"] == 3
        # The terminal line repeats the same resource counters (no new work
        # happened between the last in-loop charge and loop exit).
        terminal = lines[-1]
        assert terminal["step"] == 3
        assert terminal["tokens_used"] == 30

    async def test_the_raw_score_is_named_after_the_problems_own_scale(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """A speedup problem's raw score must never be written under the
        literal key ``"score"`` — see AC23. ``verify_every_step=False`` keeps
        the single verify inside the cap so the score-bearing line survives.
        """
        problem = make_problem("s1", scores=(3.5,))
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempt(
            problem, make_config(verify_every_step=False), output_dir=store.round_dir(0)
        )
        lines = _read_lines(store.round_dir(0) / "attempts" / "s1" / "metrics.jsonl")
        scored = [line for line in lines if problem.verifier.score_scale in line]
        assert scored, "expected at least one line carrying the raw score"
        assert scored[0][problem.verifier.score_scale] == 3.5
        assert not any("score" in line for line in lines)

    async def test_the_pass_criterion_target_produces_a_normalised_progress_series(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        problem = make_problem("s1", scores=(1.0, 5.0))
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        config = make_config(pass_criteria={"s1": PassCriterion(min_score=2.0)})
        outcome = await runner.run_attempt(problem, config, output_dir=store.round_dir(0))
        assert outcome.attempt.state is AttemptState.PASSED

        lines = _read_lines(store.round_dir(0) / "attempts" / "s1" / "metrics.jsonl")
        progress_values = [line["progress"] for line in lines if "progress" in line]
        # baseline (first score, 1.0) normalises to 0.0; the passing score
        # (5.0, above the 2.0 target) clamps to 1.0.
        assert progress_values[0] == 0.0
        assert progress_values[-1] == 1.0

    async def test_progress_svg_is_written_for_an_attempt_with_a_criterion(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Regression for the ``forcing_series="score"`` literal (Fix 3, AC23):
        that name matches no series the emitter ever writes (the raw score is
        named after the problem's own ``score_scale``), so
        ``render_attempt_plots`` silently skipped ``progress.svg`` on every
        attempt while everything else reported success — the gate observed
        this as ``research.plots.skipped filename=progress.svg``.
        """
        problem = make_problem("s1", scores=(1.0, 5.0))
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        config = make_config(pass_criteria={"s1": PassCriterion(min_score=2.0)})
        await runner.run_attempt(problem, config, output_dir=store.round_dir(0))
        plot_path = store.round_dir(0) / "attempts" / "s1" / "progress.svg"
        assert plot_path.is_file()
        assert plot_path.stat().st_size > 0

    async def test_final_line_records_the_terminal_state_not_the_pre_termination_one(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Inverse of the bug the gate caught (spec's "Terminal-line rule",
        added 2026-08-14).

        Edit C's per-step append sits *inside* the loop, so every line it
        writes necessarily carries the attempt's pre-termination state
        (``RUNNING`` here) — the post-verify ``_enforce_cap`` call, the
        pass-criterion check, and every escalate/finish path all run *after*
        that iteration's line has already been appended. Edit D now appends
        one more line, after the loop, once ``attempt.state`` has actually
        reached its terminal value. That terminal line is what makes
        ``reconcile_summary``'s ``outcome`` check satisfiable against real
        data: without it, the log's last line and the summary's terminal
        ``outcome`` disagreed on every honest run, which is exactly the
        false alarm the gate observed. This test pins the fixed behaviour:
        the file's last line carries the terminal outcome, and the line
        immediately before it still carries the pre-termination one.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_attempt(
            make_problem("s1"), make_config(), output_dir=store.round_dir(0)
        )
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        lines = _read_lines(store.round_dir(0) / "attempts" / "s1" / "metrics.jsonl")
        assert lines[-1]["outcome_code"] == int(Outcome.from_attempt_state(outcome.attempt.state))
        assert lines[-2]["outcome_code"] == int(Outcome.RUNNING)
        assert lines[-2]["outcome_code"] != lines[-1]["outcome_code"]

    async def test_the_metrics_chain_is_valid_for_a_completed_attempt(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempt(make_problem("s1"), make_config(), output_dir=store.round_dir(0))
        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        verdict = await verify_metrics_chain(metrics_dir)
        assert verdict.ok is True
        assert verdict.lines_checked == 4  # 3 in-loop steps + the Edit-D terminal line

    async def test_attempt_summary_exists_and_reconciles_with_the_metrics_log(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_attempt(
            make_problem("s1"), make_config(), output_dir=store.round_dir(0)
        )
        summary_path = store.round_dir(0) / "attempts" / "s1" / "metrics.json"
        summary = json.loads(summary_path.read_text())
        assert next(iter(summary)) == "schema_version"
        # steps_recorded is lines, not steps: the Edit-D terminal line makes
        # the two legitimately differ by one.
        assert summary["steps_recorded"] == len(outcome.steps) + 1
        lines = _read_lines(store.round_dir(0) / "attempts" / "s1" / "metrics.jsonl")
        assert summary["steps_recorded"] == len(lines)
        assert summary["consumed_steps"] == outcome.attempt.consumed.steps
        assert summary["final_state"] == outcome.attempt.state.value

        reconcile_verdict = await reconcile_summary(store.round_dir(0) / "attempts" / "s1")
        assert reconcile_verdict.ok is True, reconcile_verdict.mismatches

    async def test_the_attempt_log_checkpoints_and_trajectory_are_still_written(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The pre-existing artifacts must be unchanged in shape by this change."""
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempt(make_problem("s1"), make_config(), output_dir=store.round_dir(0))
        round_dir = store.round_dir(0)
        assert (round_dir / "attempts" / "s1.json").is_file()
        assert (round_dir / "checkpoints" / "s1.json").is_file()

    async def test_the_attempt_log_file_and_the_metrics_directory_do_not_collide(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Spec claim, verified on disk: ``attempts/s1.json`` (file, existing
        ``TrajectoryStore`` behaviour) and ``attempts/s1/`` (directory, this
        spec) are different names and coexist without clobbering each other.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempt(make_problem("s1"), make_config(), output_dir=store.round_dir(0))
        round_dir = store.round_dir(0)
        attempt_log = round_dir / "attempts" / "s1.json"
        metrics_dir = round_dir / "attempts" / "s1"
        assert attempt_log.is_file()
        assert metrics_dir.is_dir()
        assert (metrics_dir / "metrics.jsonl").is_file()

    async def test_metrics_jsonl_depth_from_the_results_root_fits_the_desktop_walk(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """``desktop/src-tauri/src/fsroots.rs`` walks to ``MAX_DEPTH = 8`` and
        skips any path segment starting with ``.``. The results root is
        ``store.loop_dir.parent`` (``self._trajectory.loop_dir.parent`` in the
        runner); count the real number of directory segments beneath it.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempt(make_problem("s1"), make_config(), output_dir=store.round_dir(0))
        results_root = store.loop_dir.parent
        metrics_path = store.round_dir(0) / "attempts" / "s1" / "metrics.jsonl"
        rel = metrics_path.relative_to(results_root)
        segments = rel.parts
        # loop-<slug> / round-00 / attempts / s1 / metrics.jsonl == 5 segments,
        # i.e. 4 directories deep -- well inside fsroots.rs's MAX_DEPTH of 8.
        assert len(segments) - 1 <= 8
        assert not any(part.startswith(".") for part in segments)

    async def test_a_raising_plot_renderer_does_not_fail_the_attempt(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        monkeypatch: object,
    ) -> None:
        """Edit D's one sanctioned swallowed error: a broken renderer must not
        turn an attempt that ran to completion into an exception.
        """

        async def _boom(*args: object, **kwargs: object) -> tuple[Path, ...]:
            raise RuntimeError("matplotlib blew up")

        monkeypatch.setattr("turing.research.loop.runner.render_attempt_plots", _boom, raising=True)
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        with structlog.testing.capture_logs() as cap:
            outcome = await runner.run_attempt(
                make_problem("s1"), make_config(), output_dir=store.round_dir(0)
            )
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        failures = [e for e in cap if e.get("event") == "research.results.emit_failed"]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "error"
        assert failures[0]["problem_id"] == "s1"
        # The summary write ran and succeeded before the plot call raised.
        assert (store.round_dir(0) / "attempts" / "s1" / "metrics.json").exists()

    async def test_a_raising_summary_writer_does_not_fail_the_attempt(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        monkeypatch: object,
    ) -> None:
        async def _boom(*args: object, **kwargs: object) -> Path:
            raise OSError("disk full")

        monkeypatch.setattr(
            "turing.research.loop.runner.write_attempt_summary", _boom, raising=True
        )
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        with structlog.testing.capture_logs() as cap:
            outcome = await runner.run_attempt(
                make_problem("s1"), make_config(), output_dir=store.round_dir(0)
            )
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert any(e.get("event") == "research.results.emit_failed" for e in cap)
        # And metrics.jsonl -- the 3 in-loop lines plus the Edit-D terminal
        # line, all written before the summary step raised -- is intact
        # regardless.
        lines = _read_lines(store.round_dir(0) / "attempts" / "s1" / "metrics.jsonl")
        assert len(lines) == 4


# --------------------------------------------------------------------------- #
# A whole round
# --------------------------------------------------------------------------- #


class TestRoundEmission:
    async def test_a_round_writes_a_round_summary_a_scores_plot_and_viewer_config(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        corpus = [make_problem("speed-1", scores=(2.0,)), make_problem("speed-2", scores=(3.0,))]
        await runner.run_round(corpus, make_config())

        round_dir = store.round_dir(0)
        summary = json.loads((round_dir / "metrics.json").read_text())
        assert next(iter(summary)) == "schema_version"
        assert len(summary["cells"]) == 1  # one (speedup, practice) cell
        assert "mean_score" in summary["cells"][0]
        assert not any(key in summary for key in ("mean_score", "total_score", "pooled_score"))

        assert (round_dir / "scores.svg").exists()

        viewer_path = store.loop_dir.parent / ".viewer.json"
        viewer = json.loads(viewer_path.read_text())
        assert viewer["series"] == "progress"
        assert any(run.endswith("attempts/speed-1") for run in viewer["runs"])
        assert any(run.endswith("attempts/speed-2") for run in viewer["runs"])
        assert all("\\" not in run for run in viewer["runs"])

    async def test_round_summary_comparable_to_parent_matches_the_trajectory_row(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Fix 4 regression. Round 0 has no parent, so "comparable to parent"
        is not a yes/no fact about it — trajectory.json's ``append_round``
        already records ``None`` for exactly this round (there is no
        previous row to compare against). The round summary previously
        hard-coded ``True`` regardless of whether a parent existed, which is
        the mismatch the gate found between the summary and the trajectory
        row for the same round.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_round([make_problem("s1", scores=(2.0,))], make_config())

        summary = json.loads((store.round_dir(0) / "metrics.json").read_text())
        trajectory_row = json.loads(store.trajectory_path.read_text())["rounds"][0]
        assert trajectory_row["comparable_to_parent"] is None
        assert summary["comparable_to_parent"] is None
        assert summary["comparable_to_parent"] == trajectory_row["comparable_to_parent"]

    async def test_a_raising_round_plot_does_not_fail_the_round(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        monkeypatch: object,
    ) -> None:
        """Regression: a plot failure used to take ``.viewer.json`` down with it.

        ``render_round_plot`` sat between ``write_round_summary`` and
        ``write_viewer_config`` in one shared ``try``, so a raise here (its
        real failure mode is ``float(cell["mean_score"])`` with no guard)
        skipped the viewer config write entirely -- an unrelated matplotlib
        problem cost the desktop pane its run list. The fix reorders the two
        JSON writes ahead of the plot and isolates the plot's own failure, so
        this must now log a *distinct* event from a genuine JSON-write
        failure (asserted in the summary-writer-raises test below) rather
        than both collapsing into the same ``round_emit_failed`` line.
        """

        async def _boom(*args: object, **kwargs: object) -> Path | None:
            raise RuntimeError("matplotlib blew up")

        monkeypatch.setattr("turing.research.loop.runner.render_round_plot", _boom, raising=True)
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        with structlog.testing.capture_logs() as cap:
            outcome = await runner.run_round([make_problem("s1")], make_config())
        assert outcome.record is not None

        plot_failures = [e for e in cap if e.get("event") == "research.results.round_plot_failed"]
        assert len(plot_failures) == 1
        assert plot_failures[0]["log_level"] == "error"
        # Not the generic JSON-write failure event -- the two must stay
        # distinguishable in an overnight log.
        assert not any(e.get("event") == "research.results.round_emit_failed" for e in cap)

        # The trajectory row -- the thing that actually matters -- still landed.
        assert store.trajectory_path.exists()
        # And so, now, does the viewer config: this is the actual bug fix.
        # It used to be silently dropped by exactly this failure.
        viewer_path = store.loop_dir.parent / ".viewer.json"
        assert viewer_path.is_file()
        viewer = json.loads(viewer_path.read_text())
        assert any(run.endswith("attempts/s1") for run in viewer["runs"])
        # scores.svg is the one artifact this failure is honestly allowed to
        # cost the round.
        assert not (store.round_dir(0) / "scores.svg").exists()

    async def test_a_raising_round_summary_writer_still_logs_round_emit_failed(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        monkeypatch: object,
    ) -> None:
        """The other half of keeping the two failures distinguishable: a
        genuine structural-JSON failure (not the plot) must still surface as
        ``round_emit_failed``, and must not be misreported as the plot's
        ``round_plot_failed`` event.
        """

        async def _boom(*args: object, **kwargs: object) -> Path:
            raise OSError("disk full")

        monkeypatch.setattr("turing.research.loop.runner.write_round_summary", _boom, raising=True)
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        with structlog.testing.capture_logs() as cap:
            outcome = await runner.run_round([make_problem("s1")], make_config())
        assert outcome.record is not None
        emit_failures = [e for e in cap if e.get("event") == "research.results.round_emit_failed"]
        assert len(emit_failures) == 1
        assert emit_failures[0]["log_level"] == "error"
        assert not any(e.get("event") == "research.results.round_plot_failed" for e in cap)
        # Neither the viewer config nor the plot ran -- the summary write is
        # upstream of both now, so a failure there honestly costs both.
        assert not (store.loop_dir.parent / ".viewer.json").exists()
        assert store.trajectory_path.exists()

    async def test_viewer_config_lists_runs_from_every_round_not_just_the_latest(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Regression: ``.viewer.json`` used to be overwritten every round with
        only that round's outcomes, so round 1's write silently dropped round
        0's attempts from the desktop pane's run list even though their files
        were still on disk. The fix derives the list from disk rather than
        the current round's in-memory ``outcomes``, so both rounds' attempt
        directories must be present after round 1 -- including the ones round
        1 did not itself touch.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        round0 = await runner.run_round(
            [make_problem("r0-a", scores=(2.0,))], make_config(round_index=0, run_id="r00")
        )

        round1 = await runner.run_round(
            [make_problem("r1-a", scores=(3.0,))],
            make_config(round_index=1, run_id="r01", parent_round_id="r00"),
            parent=round0.record,
        )
        assert round1.record is not None

        viewer_path = store.loop_dir.parent / ".viewer.json"
        viewer = json.loads(viewer_path.read_text())
        runs = viewer["runs"]
        assert any(run.endswith("round-00/attempts/r0-a") for run in runs)
        assert any(run.endswith("round-01/attempts/r1-a") for run in runs)
        # Sorted and deduplicated, not merely "both present in some order".
        assert runs == sorted(set(runs))


# --------------------------------------------------------------------------- #
# Sanity: the shared matplotlib module really is configured deterministically
# --------------------------------------------------------------------------- #


def test_plots_module_selected_the_agg_backend() -> None:
    """Cheap guard: importing turing.research.loop.runner must not have pulled
    in a GUI backend via some other import path.
    """
    import matplotlib

    assert matplotlib.get_backend().lower() == "agg"
    assert plots_module.PLOT_FILENAMES == ("progress.svg", "cap.svg")


# --------------------------------------------------------------------------- #
# Fix 5 — reconcile's happy path, driven against the real emitter
# --------------------------------------------------------------------------- #


class TestIntegrityAgainstARealRun:
    """This is the test the gate's whole finding was that nobody had written.

    ``reconcile_summary``'s only happy-path coverage before this change was a
    hand-built fixture whose last line carried a terminal ``outcome_code`` —
    a line shape ``run_attempt`` never actually produced (see the
    terminal-line tests above). The unit suite tested the fixture, not the
    emitter. These tests drive the real, unmocked ``RoundRunner`` and assert
    both integrity checks pass on what it actually writes, for both shapes an
    attempt can end in: passing against a ``PassCriterion``, and running out
    the cap with none set.
    """

    async def test_chain_and_reconcile_are_both_ok_with_a_pass_criterion(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        problem = make_problem("s1", scores=(1.0, 5.0))
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        config = make_config(pass_criteria={"s1": PassCriterion(min_score=2.0)})
        outcome = await runner.run_attempt(problem, config, output_dir=store.round_dir(0))
        assert outcome.attempt.state is AttemptState.PASSED

        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        chain_verdict = await verify_metrics_chain(metrics_dir)
        assert chain_verdict.ok is True, chain_verdict.reason
        reconcile_verdict = await reconcile_summary(metrics_dir)
        assert reconcile_verdict.ok is True, reconcile_verdict.mismatches

    async def test_a_redrive_into_the_same_output_dir_rotates_the_prior_attempt_and_both_verify(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Round-4 regression: the ``MetricsWriter`` refusal, driven for real.

        ``run_attempt``, ``run_round``, and ``NoiseFloorRunner.run`` all have
        zero skip-if-already-done logic, so a re-drive of an attempt into an
        already-used ``output_dir`` — a closed subscription window, a killed
        process, a re-driven round — is legitimate and reachable. Without a
        call-site fix, the second ``run_attempt`` below would raise
        ``ContractViolationError`` from ``MetricsWriter.__init__`` refusing
        to splice a second header onto the first attempt's chain.

        This drives two real attempts, unmocked, into the same directory for
        the same problem and asserts: the current (second) attempt's chain
        and summary verify clean; the first attempt's trio survived on disk
        rather than being deleted or spliced onto; and — since the fix
        rotates the whole trio together, not just the JSONL — the
        rotated-aside copy is *itself* a complete, independently verifiable
        trio. A rotation that preserves an unverifiable file is only half a
        fix.
        """
        problem = make_problem("s1")
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        output_dir = store.round_dir(0)

        first = await runner.run_attempt(problem, make_config(), output_dir=output_dir)
        assert first.attempt.state is AttemptState.FAILED_WITHIN_CAP

        second = await runner.run_attempt(problem, make_config(), output_dir=output_dir)
        assert second.attempt.state is AttemptState.FAILED_WITHIN_CAP
        assert second.attempt.attempt_id != first.attempt.attempt_id

        metrics_dir = output_dir / "attempts" / "s1"

        # The current (second) attempt's chain and summary are intact.
        chain_verdict = await verify_metrics_chain(metrics_dir)
        assert chain_verdict.ok is True, chain_verdict.reason
        reconcile_verdict = await reconcile_summary(metrics_dir)
        assert reconcile_verdict.ok is True, reconcile_verdict.mismatches

        # The first attempt's trio survived, rotated aside into exactly one
        # numbered subdirectory rather than deleted or spliced onto.
        rotated_dirs = sorted(p for p in metrics_dir.iterdir() if p.is_dir())
        assert len(rotated_dirs) == 1, f"expected one rotated-aside dir, got {rotated_dirs}"
        rotated_dir = rotated_dirs[0]
        for name in ("metrics.jsonl", "metrics.chain.json", "metrics.json"):
            assert (rotated_dir / name).is_file(), f"{name} missing from {rotated_dir}"

        # The rotated-aside copy actually belongs to the first attempt (by
        # header identity), not a stray duplicate of the second's.
        rotated_sidecar = json.loads((rotated_dir / "metrics.chain.json").read_text())
        current_sidecar = json.loads((metrics_dir / "metrics.chain.json").read_text())
        assert rotated_sidecar["header"]["attempt_id"] == first.attempt.attempt_id
        assert current_sidecar["header"]["attempt_id"] == second.attempt.attempt_id

        # The rotated-aside copy is itself a full, independently verifiable
        # trio, not just a JSONL file kept for looks.
        rotated_chain_verdict = await verify_metrics_chain(rotated_dir)
        assert rotated_chain_verdict.ok is True, rotated_chain_verdict.reason
        rotated_reconcile_verdict = await reconcile_summary(rotated_dir)
        assert rotated_reconcile_verdict.ok is True, rotated_reconcile_verdict.mismatches

    async def test_chain_and_reconcile_are_both_ok_when_the_solver_raises_with_accounted_tokens(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Round-2 regression: the exact shape the gate reproduced.

        A solver whose exception carries accounted tokens (``exc.tokens``)
        drives ``_charge_failed_step`` -> ``_spend_carried_on_error``, which
        bumps ``attempt.consumed.steps`` without ever bumping
        ``attempt.step_index`` — the step never happened, so there is
        nothing to advance the chart's x-axis, but the tokens were spent and
        must still be charged against the cap. Before ``consumed_steps``
        became its own emitted field, reconcile derived it from the last
        line's ``step`` (still ``0`` here) and raised a false
        ``consumed_steps`` mismatch against real data (``ABANDONED``,
        ``step_index=0``, ``consumed.steps=2``, in the gate's own repro).
        """
        channel = ScriptedEscalationChannel([EscalationVerdict.ABANDON])
        runner = make_runner(
            solver=_RaisingSolverWithAccountedTokens(tokens=77),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1"), make_config(), output_dir=store.round_dir(0)
        )
        assert outcome.attempt.state is AttemptState.ABANDONED
        assert outcome.attempt.step_index == 0
        assert outcome.attempt.consumed.steps > 0
        assert outcome.attempt.consumed.steps != outcome.attempt.step_index

        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        chain_verdict = await verify_metrics_chain(metrics_dir)
        assert chain_verdict.ok is True, chain_verdict.reason
        reconcile_verdict = await reconcile_summary(metrics_dir)
        assert reconcile_verdict.ok is True, reconcile_verdict.mismatches

        lines = _read_lines(metrics_dir / "metrics.jsonl")
        # The terminal (and, here, only) line carries the divergence directly:
        # step stays the solver's step index while consumed_steps is cap
        # accounting, and the two are not the same number.
        assert lines[-1]["step"] == 0
        assert lines[-1]["consumed_steps"] == outcome.attempt.consumed.steps
        assert lines[-1]["consumed_steps"] != lines[-1]["step"]

    async def test_chain_and_reconcile_are_both_ok_when_the_solver_raises_with_no_accounted_tokens(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The other exception shape: a plain error with nothing on
        ``.usage``/``.tokens``. ``_spend_carried_on_error`` then charges
        nothing, so ``consumed_steps`` stays ``0`` alongside ``step_index=0``
        — this is the degenerate case where the two quantities do coincide,
        and reconcile must still pass.
        """
        channel = ScriptedEscalationChannel([EscalationVerdict.ABANDON])
        runner = make_runner(
            solver=ExplodingSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=channel,
        )
        outcome = await runner.run_attempt(
            make_problem("s1"), make_config(), output_dir=store.round_dir(0)
        )
        assert outcome.attempt.state is AttemptState.ABANDONED
        assert outcome.attempt.step_index == 0
        assert outcome.attempt.consumed.steps == 0

        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        chain_verdict = await verify_metrics_chain(metrics_dir)
        assert chain_verdict.ok is True, chain_verdict.reason
        reconcile_verdict = await reconcile_summary(metrics_dir)
        assert reconcile_verdict.ok is True, reconcile_verdict.mismatches

    async def test_chain_and_reconcile_are_both_ok_without_a_pass_criterion(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """No criterion: the attempt runs to the cap and lands in
        ``FAILED_WITHIN_CAP`` — this is the exact shape the gate drove and
        found failing 100% of the time (mismatches on ``outcome`` and, in one
        of its two runs, ``baseline_score``).
        """
        problem = make_problem("s1")
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0))
        assert outcome.attempt.state is AttemptState.FAILED_WITHIN_CAP

        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        chain_verdict = await verify_metrics_chain(metrics_dir)
        assert chain_verdict.ok is True, chain_verdict.reason
        reconcile_verdict = await reconcile_summary(metrics_dir)
        assert reconcile_verdict.ok is True, reconcile_verdict.mismatches


# --------------------------------------------------------------------------- #
# Round-5 regression — a PassCriterion whose min_score is not finite
# --------------------------------------------------------------------------- #


class TestNonFiniteTargetAttempts:
    """The fourth false alarm this change produced, driven for real.

    ``PassCriterion(min_score=float("nan"))`` — and the same for ``±inf`` — is
    constructible: ``PassCriterion.__post_init__`` checks only that the
    criterion is *a* bar (not both ``min_score=None`` and
    ``require_correctness=False``), never that ``min_score`` is a finite
    number, unlike its sibling ``VerificationResult.__post_init__`` which does
    exactly that check on ``score``.

    Round 4 fixed the two things that value broke on the way through —
    ``ProgressTracker`` refuses to normalise against it, and
    ``_write_summary_json`` sets ``allow_nan=False`` so it can never be
    written as a bare ``NaN`` token — and the two fixes then collided. The
    target went into the summary payload raw, ``json.dumps`` raised on it,
    ``runner.py``'s sanctioned reporting guard swallowed that as
    ``research.results.emit_failed`` exactly as designed, and the summary
    silently never landed. ``verify`` then reported ``metrics.json is
    missing`` and exited ``1`` on a completely honest run.

    These tests drive real, unmocked attempts. They assert the artifacts all
    tell the same story — no target, no progress, and a file that verifies —
    rather than asserting the verifier has been taught to overlook the gap.
    """

    @pytest.mark.parametrize("bad_target", [float("nan"), float("inf"), float("-inf")])
    async def test_a_non_finite_target_still_lands_a_summary_that_verifies(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        bad_target: float,
    ) -> None:
        problem = make_problem("s1", scores=(1.0, 5.0))
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        config = make_config(pass_criteria={"s1": PassCriterion(min_score=bad_target)})
        with structlog.testing.capture_logs() as cap:
            outcome = await runner.run_attempt(problem, config, output_dir=store.round_dir(0))
        assert outcome.attempt.is_terminal

        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        summary_path = metrics_dir / "metrics.json"
        assert summary_path.is_file(), "the summary must land even on a degenerate target"
        summary_text = summary_path.read_text()
        # Both halves matter: the file exists *and* it is real JSON. Dropping
        # allow_nan=False would also make it exist, as a file only Python can
        # read, while reconcile stayed green over it.
        assert "NaN" not in summary_text
        assert "Infinity" not in summary_text
        summary = json.loads(summary_text)
        assert summary["target_score"] is None
        assert summary["final_progress"] is None

        lines = _read_lines(metrics_dir / "metrics.jsonl")
        assert lines
        assert not any("progress" in line for line in lines), (
            "ProgressTracker refused this target, so no line may claim a progress reading"
        )

        degenerate = [e for e in cap if e.get("event") == "research.results.degenerate_target"]
        assert len(degenerate) == 1, "once per tracker, not once per step"
        assert degenerate[0]["log_level"] == "warning"
        dropped = [e for e in cap if e.get("event") == "research.results.target_score_dropped"]
        assert len(dropped) == 1
        assert dropped[0]["log_level"] == "warning"
        # The whole point: nothing failed, so nothing may be reported as having
        # failed. This is the log line that used to be here instead of the file.
        assert not [e for e in cap if e.get("event") == "research.results.emit_failed"]

        chain_verdict = await verify_metrics_chain(metrics_dir)
        assert chain_verdict.ok is True, chain_verdict.reason
        reconcile_verdict = await reconcile_summary(metrics_dir)
        assert reconcile_verdict.ok is True, reconcile_verdict.mismatches
        assert await _verify_exit_code(metrics_dir) == 0

    async def test_a_redrive_over_a_non_finite_target_attempt_verifies_in_both_generations(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """The compound shape round 4's gate found: ``prior-1`` carrying the
        poison forward.

        A re-drive rotates the previous attempt's trio into ``prior-N/``
        rather than deleting it, and ``verify``'s walk matches
        ``metrics.jsonl`` at every depth — so the rotated generation is
        checked too. When the first attempt never managed to write a
        ``metrics.json``, the rotated directory was a permanently
        unverifiable run sitting inside an otherwise clean results root, and
        the honest repair run inherited a red verdict from it forever. Here
        the operator's first configuration is the degenerate one and the
        second fixes it; both generations must verify.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        output_dir = store.round_dir(0)

        first = await runner.run_attempt(
            make_problem("s1", scores=(1.0, 5.0)),
            make_config(pass_criteria={"s1": PassCriterion(min_score=float("nan"))}),
            output_dir=output_dir,
        )
        second = await runner.run_attempt(
            make_problem("s1", scores=(1.0, 5.0)),
            make_config(pass_criteria={"s1": PassCriterion(min_score=2.0)}),
            output_dir=output_dir,
        )
        assert second.attempt.attempt_id != first.attempt.attempt_id
        assert second.attempt.state is AttemptState.PASSED

        metrics_dir = output_dir / "attempts" / "s1"
        rotated_dirs = sorted(p for p in metrics_dir.iterdir() if p.is_dir())
        assert len(rotated_dirs) == 1, f"expected one rotated-aside dir, got {rotated_dirs}"
        prior_dir = rotated_dirs[0]
        for name in ("metrics.jsonl", "metrics.chain.json", "metrics.json"):
            assert (prior_dir / name).is_file(), f"{name} missing from {prior_dir}"

        prior_summary = json.loads((prior_dir / "metrics.json").read_text())
        assert prior_summary["attempt_id"] == first.attempt.attempt_id
        assert prior_summary["target_score"] is None
        current_summary = json.loads((metrics_dir / "metrics.json").read_text())
        assert current_summary["attempt_id"] == second.attempt.attempt_id
        assert current_summary["target_score"] == 2.0

        for directory in (metrics_dir, prior_dir):
            chain_verdict = await verify_metrics_chain(directory)
            assert chain_verdict.ok is True, f"{directory}: {chain_verdict.reason}"
            reconcile_verdict = await reconcile_summary(directory)
            assert reconcile_verdict.ok is True, f"{directory}: {reconcile_verdict.mismatches}"

        # And the operator's actual command: one exit code over the whole
        # results root, covering both generations at once.
        assert await _verify_exit_code(store.loop_dir.parent) == 0


# --------------------------------------------------------------------------- #
# Round-6 regression — one bad attempt must not abort the whole round
# --------------------------------------------------------------------------- #


class TestOneBadAttemptDoesNotAbortTheRound:
    """A round is ~11 problems run sequentially over hours of subscription time.

    Before the fix, ``run_attempts`` called ``run_attempt`` with no per-attempt
    guard, so the first problem to raise discarded every attempt the round had
    already completed: no round record, no trajectory row, no cells, and the
    machine time already spent on the earlier problems unrecoverable. The
    reachable trigger needs no adversarial input — ``score_scale`` is free-form
    by design, it becomes the metrics key naming the raw score, and
    ``progress`` / ``step`` / ``ts`` / ``tokens_used`` / ``consumed_steps`` /
    ``wall_clock_s`` (and ~10 more) are all rejected by
    ``results._validate_scored_metric`` at that problem's *first* verification.

    The validation is right and stays strict: a scale that silently clobbered
    a core field would corrupt every chart drawn off the file. What changed is
    the blast radius. These tests drive a real, unmocked ``RoundRunner``
    against a real colliding scale.
    """

    async def test_a_colliding_score_scale_fails_only_its_own_problem(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        corpus = [
            make_problem("good-1", scores=(2.0,)),
            _problem_with_scale("bad", "progress"),
            make_problem("good-2", scores=(3.0,)),
        ]
        with structlog.testing.capture_logs() as cap:
            outcome = await runner.run_round(corpus, make_config())

        # The round finished and was recorded at all -- this is the whole fix.
        assert store.trajectory_path.is_file()
        assert outcome.trajectory_row is not None

        # ``good-2`` comes *after* the failure in corpus order, so its metrics
        # directory existing is the proof the loop kept going rather than
        # unwinding at the first raise.
        assert (store.round_dir(0) / "attempts" / "good-2" / "metrics.jsonl").is_file()

        assert [o.problem.id for o in outcome.attempts] == ["good-1", "good-2"]
        assert [f.problem_id for f in outcome.failures] == ["bad"]
        assert "ContractViolationError" in outcome.failures[0].error

        crashed = [e for e in cap if e.get("event") == "research.attempt.crashed"]
        assert len(crashed) == 1
        assert crashed[0]["log_level"] == "error"
        assert crashed[0]["problem_id"] == "bad"
        lost = [e for e in cap if e.get("event") == "research.round.attempts_lost"]
        assert len(lost) == 1
        assert lost[0]["log_level"] == "error"
        assert lost[0]["lost"] == ["bad"]

    async def test_the_lost_problem_is_absent_from_the_cells_never_floored_into_them(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The containment must not buy round completion with a fake datum.

        Synthesising an outcome with ``best_result=None`` for the crashed
        attempt is exactly the shape of an honest attempt that ran its cap and
        never scored, so it would enter the cell at the scale floor and average
        an instrument failure in as a capability reading -- turning a bug in
        ``runner.py`` into a regression against the parent round. The cell here
        must therefore read as a two-problem cell with the two real scores,
        identical to what the same corpus minus the bad problem would produce.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        corpus = [
            make_problem("good-1", scores=(2.0,)),
            _problem_with_scale("bad", "tokens_used"),
            make_problem("good-2", scores=(3.0,)),
        ]
        outcome = await runner.run_round(corpus, make_config())

        assert [s.problem_id for s in outcome.scored] == ["good-1", "good-2"]
        assert len(outcome.record.type_scores) == 1
        cell = outcome.record.type_scores[0]
        assert set(cell.scores) == {"good-1", "good-2"}
        assert cell.mean_score == pytest.approx(2.5)
        # Not merely "not floored": the floor for this scale is 0.0, so a
        # floored third entry would drag the mean to ~1.67 and n to 3.
        assert cell.n == 2

        # Cost is computed over the same set the cells are, so #3's numerator
        # and denominator cannot be taken over different problems.
        assert outcome.record.cost.attempts == 2

    async def test_the_round_record_and_trajectory_row_both_say_an_attempt_was_lost(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Absent must not mean silent.

        Every other number in the record is computed over the attempts that
        survived, so without an explicit signal a round of two measured
        problems and a round of three are indistinguishable on disk. The gate
        carries it into both the round record and ``trajectory.json``'s
        per-round ``constraints``; the verdict carries it into the one line an
        operator actually reads off a trajectory row.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        corpus = [make_problem("good-1", scores=(2.0,)), _problem_with_scale("bad", "step")]
        outcome = await runner.run_round(corpus, make_config())

        assert outcome.record.gates["all_attempts_completed"] is False
        assert "bad" in outcome.record.verdict
        assert outcome.record.verdict.startswith("1 of 2 attempt(s) failed")

        row = json.loads(store.trajectory_path.read_text())["rounds"][0]
        assert row["constraints"]["all_attempts_completed"] is False
        assert row["verdict"] == outcome.record.verdict

    async def test_a_clean_round_still_reports_the_gate_as_true(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The negative half: the new gate and verdict prefix must not appear on
        a round where nothing went wrong, or they stop meaning anything.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = await runner.run_round(
            [make_problem("s1", scores=(2.0,)), make_problem("s2", scores=(3.0,))], make_config()
        )
        assert outcome.record.gates["all_attempts_completed"] is True
        assert outcome.failures == ()
        assert "failed and are absent" not in outcome.record.verdict
        assert outcome.record.cost.attempts == 2

    async def test_a_round_whose_every_attempt_fails_is_refused_not_recorded(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Containment is not "record whatever survived, even if that is nothing".

        A round record with zero cells is a baseline that measured nothing, and
        the next round would compute its deltas against that absence. Refusing
        is the honest outcome, and the losses are still readable off the runner
        afterwards.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        corpus = [_problem_with_scale("bad-1", "progress"), _problem_with_scale("bad-2", "ts")]
        with pytest.raises(ContractViolationError, match="every one of the 2 attempt"):
            await runner.run_round(corpus, make_config())

        assert [f.problem_id for f in runner.attempt_failures] == ["bad-1", "bad-2"]
        # Nothing was recorded: no round row claiming a baseline that is not one.
        assert (await store.load_trajectory())["rounds"] == []

    async def test_a_cancelled_attempt_stops_the_round_instead_of_being_contained(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """``except Exception``, not ``except BaseException``, and it matters.

        ``asyncio.CancelledError`` is a ``BaseException``: catching it would
        turn "this round was cancelled" into "problem s1 had a harness
        failure" and then keep spending subscription time on the rest of the
        corpus, which is precisely what a cancellation is asking not to happen.
        """
        solver = _CancellingSolver()
        runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
        corpus = [make_problem("s1"), make_problem("s2")]
        with pytest.raises(asyncio.CancelledError):
            await runner.run_attempts(corpus, make_config(), output_dir=store.round_dir(0))

        assert solver.calls == 1  # stopped at the first problem, did not go on to s2
        assert not (store.round_dir(0) / "attempts" / "s2").exists()


# --------------------------------------------------------------------------- #
# Round-6 regression — a stale progress.svg must not outlive a re-drive
# --------------------------------------------------------------------------- #


class TestARedriveLeavesNoStalePlot:
    """Rotation used to move only the chained trio.

    Its docstring justified that by claiming everything else under the attempt
    directory "is a wholesale overwrite that self-heals on a re-drive". True of
    the checkpoints and the attempt log; **false of the plots**.
    ``render_attempt_plots`` *skips* -- writes nothing -- when the series a
    plot needs is absent from every point, so a re-driven attempt that dies
    before its first usable verification rewrites ``cap.svg`` (``tokens_used``
    is on every line, terminal one included) and leaves the previous attempt's
    ``progress.svg`` untouched beside it. The desktop's images pane then draws
    attempt 1's progress curve next to attempt 2's cap chart with nothing
    marking either as stale -- the same "one chart drawn out of two attempts"
    failure rotation was built to prevent, displaced from the JSONL to the SVG.
    """

    async def test_a_redrive_that_never_verifies_leaves_no_plot_from_the_prior_attempt(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        output_dir = store.round_dir(0)
        metrics_dir = output_dir / "attempts" / "s1"

        first_runner = make_runner(
            solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock
        )
        first = await first_runner.run_attempt(
            make_problem("s1", scores=(1.0, 5.0)),
            make_config(pass_criteria={"s1": PassCriterion(min_score=2.0)}),
            output_dir=output_dir,
        )
        assert first.attempt.state is AttemptState.PASSED
        first_plots = {
            name: (metrics_dir / name).read_bytes() for name in plots_module.PLOT_FILENAMES
        }

        # The re-drive: a verifier that raises, escalates, and is abandoned --
        # so the attempt reaches its terminal state having never produced a
        # score, and therefore never a ``progress`` point.
        second_runner = make_runner(
            solver=FakeSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=ScriptedEscalationChannel([EscalationVerdict.ABANDON]),
        )
        second = await second_runner.run_attempt(
            make_problem("s1", raises=True),
            make_config(pass_criteria={"s1": PassCriterion(min_score=2.0)}),
            output_dir=output_dir,
        )
        assert second.attempt.state is AttemptState.ABANDONED
        assert second.attempt.attempt_id != first.attempt.attempt_id

        # cap.svg is the current attempt's (tokens_used is on every line);
        # progress.svg is simply absent, which is the honest reading of "this
        # attempt never produced a progress series". The old bug is the
        # inverse: progress.svg present and belonging to somebody else.
        rendered = sorted(p.name for p in metrics_dir.glob("*.svg"))
        assert rendered == ["cap.svg"]
        assert (metrics_dir / "cap.svg").read_bytes() != first_plots["cap.svg"]

    async def test_the_prior_attempts_plots_are_preserved_beside_their_own_metrics(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Preserved, not deleted -- matching the trio's rotation philosophy.

        A stale chart is only misleading where the *current* attempt's chart
        belongs. Under ``prior-N/``, beside the exact metrics it was drawn
        from, it is the superseded generation's evidence and its path says so.
        The rotated directory must also stay verifiable: ``verify``'s walk
        keys on ``metrics.jsonl`` alone, so the SVGs joining it change nothing
        about what that walk finds or reports.
        """
        output_dir = store.round_dir(0)
        metrics_dir = output_dir / "attempts" / "s1"
        config = make_config(pass_criteria={"s1": PassCriterion(min_score=2.0)})

        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        first = await runner.run_attempt(
            make_problem("s1", scores=(1.0, 5.0)), config, output_dir=output_dir
        )
        first_plots = {
            name: (metrics_dir / name).read_bytes() for name in plots_module.PLOT_FILENAMES
        }

        second = await runner.run_attempt(
            make_problem("s1", scores=(1.0, 5.0)), config, output_dir=output_dir
        )
        assert second.attempt.attempt_id != first.attempt.attempt_id

        rotated_dirs = sorted(p for p in metrics_dir.iterdir() if p.is_dir())
        assert len(rotated_dirs) == 1, f"expected one rotated-aside dir, got {rotated_dirs}"
        prior_dir = rotated_dirs[0]
        for name, data in first_plots.items():
            assert (prior_dir / name).read_bytes() == data, f"{name} is not the first attempt's"

        # The rotated generation is still a complete, independently checkable
        # run -- the SVGs did not displace or shadow the trio.
        for name in ("metrics.jsonl", "metrics.chain.json", "metrics.json"):
            assert (prior_dir / name).is_file()
        assert (await verify_metrics_chain(prior_dir)).ok is True
        assert (await reconcile_summary(prior_dir)).ok is True
        assert sorted(p.name for p in verify_cli.find_runs(metrics_dir)) == ["prior-1", "s1"]

    async def test_a_first_attempt_into_a_clean_directory_rotates_nothing(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The trigger is unchanged: no prior chain, no rotation, no ``prior-N``.

        Widening rotation from three names to five must not make it fire where
        it did not before -- a first attempt would otherwise sweep an empty
        directory aside on every run.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempt(make_problem("s1"), make_config(), output_dir=store.round_dir(0))
        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        assert [p for p in metrics_dir.iterdir() if p.is_dir()] == []


# --------------------------------------------------------------------------- #
# Round-7 regression -- a contained loss must not manufacture a marginal gain
# --------------------------------------------------------------------------- #


class _WorkspacesThatFailForSomeProblems(TempWorkspaceProvider):
    """Raises out of ``materialise`` for named problem ids.

    A transient the corpus fingerprint cannot see, which is the point: a
    colliding ``score_scale`` is part of ``fingerprint_corpus``'s material, so
    a round made to lose an attempt *that* way is also a round measured on a
    different ``eval_set_hash``, and the existing eval-set refusal would mask
    the defect under test. A vanished workspace template -- a full disk, a
    pruned scratch mount, an evicted container volume -- loses exactly one
    attempt while leaving the corpus, and therefore the eval-set hash,
    byte-for-byte identical between the two rounds. That is the shape that
    reaches the round-over-round delta.
    """

    def __init__(self, root: Path, *, fail: set[str] | None = None) -> None:
        super().__init__(root)
        self.fail: set[str] = set(fail or ())

    async def materialise(self, problem: Problem, *, attempt_id: str) -> Path:
        if problem.id in self.fail:
            raise OSError(f"workspace template for {problem.id} is gone")
        return await super().materialise(problem, attempt_id=attempt_id)


class TestAContainedLossManufacturesNoGain:
    """A mean rises when its weakest member goes missing.

    Round 0 measures ``alpha`` at 3.0 and ``beta`` at 0.5, a cell mean of
    1.75 over ``n=2``. Round 1 runs the *identical* corpus with the
    *identical* scores and loses ``beta`` to a contained harness failure, so
    its cell is 3.0 over ``n=1``. The agent did not improve by any amount; the
    weaker problem dropped out of the average. Before this fix the round
    reported ``eval_set_stable: True``, ``comparable_to_parent: True`` and a
    verdict line reading "a marginal gain of +1.25" -- an improvement
    manufactured entirely by a bug in the harness, which is worse than a
    harness that crashes.

    ``noise_floor.py`` already makes this argument for a seed that lost an
    attempt: a spread over a shifting problem set measures the set, and
    nothing downstream can separate the two again. These tests are that
    argument applied where it decides whether the program is working.
    """

    @staticmethod
    def _corpus() -> list[Problem]:
        return [make_problem("alpha", scores=(3.0,)), make_problem("beta", scores=(0.5,))]

    async def _rounds(
        self,
        store: TrajectoryStore,
        clock: FakeClock,
        workspaces: _WorkspacesThatFailForSomeProblems,
        *,
        lose: set[str],
    ) -> tuple[Any, Any]:
        corpus = self._corpus()
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        first = await runner.run_round(corpus, make_config())
        workspaces.fail = lose
        second = await runner.run_round(
            corpus,
            make_config(round_index=1, run_id="r01", parent_round_id="r00"),
            parent=first.record,
            noise_floor=floors_for(corpus, {1: 1.0, 2: 1.1, 3: 1.2}),
        )
        return first, second

    async def test_the_round_that_lost_a_problem_emits_no_delta_at_all(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Refused, not flagged. A number that exists will be read.

        The floor is present and the eval set is unchanged, so every other
        gate for a delta is satisfied -- this asserts the *only* thing
        stopping the +1.25 is the lost attempt.
        """
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        first, second = await self._rounds(store, clock, failing, lose={"beta"})

        # The arithmetic that would produce the phantom gain is real: the
        # parent's mean is over both problems, the child's over one.
        assert first.record.type_scores[0].mean_score == pytest.approx(1.75)
        assert first.record.type_scores[0].n == 2
        assert second.record.type_scores[0].mean_score == pytest.approx(3.0)
        assert second.record.type_scores[0].n == 1

        assert second.record.deltas == ()
        assert {a.verdict for a in second.assessments} == {SaturationVerdict.REFUSED_ATTEMPT_LOST}
        # No gain is reported anywhere in the one line an operator reads, not
        # even as a refusal's supporting number.
        assert "marginal gain" not in second.record.verdict
        assert "1.25" not in second.record.verdict
        assert all(a.marginal_gain is None for a in second.assessments)

    async def test_every_machine_readable_artifact_says_the_delta_is_not_a_comparison(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Prose in a verdict string is not a disclosure a program can act on.

        A downstream reader -- the desktop pane, a pre-writeup gate, the
        self-edit seam -- has to be able to tell that this round's delta is
        not a valid comparison without parsing English.
        """
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        await self._rounds(store, clock, failing, lose={"beta"})

        row = json.loads(store.trajectory_path.read_text())["rounds"][1]
        assert row["delta"] == {}
        assert row["delta_in_noise_units"] == {}
        assert row["cost_per_point"] == {}
        assert [s["verdict"] for s in row["saturation"]] == ["refused_attempt_lost"]
        assert row["constraints"]["all_attempts_completed"] is False

        summary = json.loads((store.round_dir(1) / "metrics.json").read_text())
        assert summary["comparable_to_parent"] is False
        assert [c for c in summary["cells"] if "marginal_gain" in c] == []

    async def test_the_eval_set_gate_still_reports_only_what_it_measures(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The two facts stay separate, and the asymmetry is deliberate.

        ``eval_set_stable`` answers "did the parent measure the same corpus?"
        and the answer here is genuinely yes -- collapsing it into the delta
        refusal would report an eval-set change that did not happen, and
        ``trajectory.py`` derives ``trajectory_restart`` from exactly that
        question. A lost attempt does not restart the trajectory: the next
        round still descends from this one.
        """
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        _, second = await self._rounds(store, clock, failing, lose={"beta"})

        assert second.record.gates["eval_set_stable"] is True
        row = json.loads(store.trajectory_path.read_text())["rounds"][1]
        assert row["trajectory_restart"] is False
        assert row["comparable_to_parent"] is True
        assert (
            row["eval_set_hash"]
            == json.loads(store.trajectory_path.read_text())["rounds"][0]["eval_set_hash"]
        )

    async def test_a_round_that_lost_nothing_still_gets_its_delta(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The negative half: the clean path must be unchanged.

        Same corpus, same floor, same lineage -- nothing crashes. The delta,
        the saturation call and ``comparable_to_parent`` must all be exactly
        what they were before the refusal existed, or the refusal has bought
        honesty by breaking the measurement it was protecting.
        """
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        _, second = await self._rounds(store, clock, failing, lose=set())

        assert second.failures == ()
        assert second.record.gates["all_attempts_completed"] is True
        assert len(second.record.deltas) == 1
        assert second.record.deltas[0].marginal_gain == pytest.approx(0.0)
        assert {a.verdict for a in second.assessments} == {SaturationVerdict.SATURATED}
        summary = json.loads((store.round_dir(1) / "metrics.json").read_text())
        assert summary["comparable_to_parent"] is True
        assert all("marginal_gain" in c for c in summary["cells"])


# --------------------------------------------------------------------------- #
# Round-8 regression -- the same phantom gain, displaced onto the parent
# --------------------------------------------------------------------------- #


class TestAParentSideLossManufacturesNoGainEither:
    """The refusal above was one-sided, and the bias simply moved one round.

    Nothing consulted the *parent* record's ``all_attempts_completed`` gate,
    so a round that lost an attempt correctly refused its own deltas and then
    served as the baseline for the next round's. Driven end-to-end: round 0
    loses ``alpha`` (3.0, the strong problem) and records 0.5 over ``n=1``;
    round 1 runs the identical corpus with the identical agent and identical
    scores, records 1.75 over ``n=2``, and emitted ``marginal_gain=+1.25``,
    ``cost_per_unit_gain``, ``IMPROVING`` and ``comparable_to_parent: true``
    with every gate green. The agent did not change at all -- the entire delta
    is *which problems ran in the parent*. Losing the weak problem in the
    parent manufactures the mirror-image phantom regression.
    """

    @staticmethod
    def _corpus() -> list[Problem]:
        return [make_problem("alpha", scores=(3.0,)), make_problem("beta", scores=(0.5,))]

    async def _two_rounds(
        self,
        store: TrajectoryStore,
        clock: FakeClock,
        workspaces: _WorkspacesThatFailForSomeProblems,
        *,
        lose_in_parent: set[str],
        lose_in_child: set[str] = frozenset(),  # type: ignore[assignment]
        strip_parent_gate: bool = False,
    ) -> tuple[Any, Any]:
        corpus = self._corpus()
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        workspaces.fail = set(lose_in_parent)
        first = await runner.run_round(corpus, make_config())
        parent = first.record
        if strip_parent_gate:
            # An older record: written before the gate existed, so it is
            # silent about the one fact the refusal turns on. Derived from a
            # real driven round rather than hand-built, because everything
            # else about it -- the cells, the lineage, the eval-set hash --
            # has to be exactly what a real parent carries.
            parent = dataclasses.replace(parent, gates={})
        workspaces.fail = set(lose_in_child)
        second = await runner.run_round(
            corpus,
            make_config(round_index=1, run_id="r01", parent_round_id="r00"),
            parent=parent,
            noise_floor=floors_for(corpus, {1: 1.0, 2: 1.1, 3: 1.2}),
        )
        return first, second

    async def test_a_complete_round_reports_no_gain_against_a_lossy_parent(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The delta the fix exists to stop, on the side it did not cover.

        The floor is present, the eval set is unchanged and *this* round lost
        nothing -- every other gate for a delta is satisfied, so this asserts
        the only thing standing between the arithmetic and the trajectory is
        the parent's own lost attempt.
        """
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        first, second = await self._two_rounds(store, clock, failing, lose_in_parent={"alpha"})

        # The arithmetic that would produce the phantom +1.25 is real.
        assert first.record.type_scores[0].mean_score == pytest.approx(0.5)
        assert first.record.type_scores[0].n == 1
        assert second.record.type_scores[0].mean_score == pytest.approx(1.75)
        assert second.record.type_scores[0].n == 2

        assert second.record.deltas == ()
        assert {a.verdict for a in second.assessments} == {
            SaturationVerdict.REFUSED_PARENT_ATTEMPT_LOST
        }
        assert all(a.marginal_gain is None for a in second.assessments)
        assert "1.25" not in second.record.verdict
        assert "marginal gain" not in second.record.verdict
        assert "parent round lost at least one attempt" in second.record.verdict

    async def test_the_refusal_names_the_parent_so_the_operator_re_drives_the_right_round(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Two refusals, because two different rounds have to be re-driven.

        ``refused_attempt_lost`` says "re-drive the problem this round lost";
        ``refused_parent_attempt_lost`` says "this round is fine, the baseline
        is not". One shared verdict would send an operator to re-run a round
        that is already complete and watch the same refusal come back.
        """
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        _, second = await self._two_rounds(store, clock, failing, lose_in_parent={"alpha"})

        row = json.loads(store.trajectory_path.read_text())["rounds"][1]
        assert [s["verdict"] for s in row["saturation"]] == ["refused_parent_attempt_lost"]
        assert row["delta"] == {}
        assert row["delta_in_noise_units"] == {}
        assert row["cost_per_point"] == {}
        # This round measured everything it set out to, and says so.
        assert second.record.gates["all_attempts_completed"] is True
        assert row["constraints"]["all_attempts_completed"] is True
        # The eval set never changed either: a lossy parent is not a restart.
        assert row["trajectory_restart"] is False
        assert second.record.gates["eval_set_stable"] is True
        # The parent's completeness is not a gate on *this* round's record --
        # every entry there is a statement about this round, and a false one
        # whose subject is a different round reads as this round failing.
        assert set(second.record.gates) == {
            "eval_set_stable",
            "lineage_recorded",
            "noise_floor_available",
            "all_attempts_completed",
        }

    async def test_the_round_summary_says_these_numbers_are_not_comparable(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """``comparable_to_parent`` answers "may these be compared with the parent's?"

        A matching eval-set hash is necessary and not sufficient: the parent's
        cells were reduced over a strict subset of this round's problems, so
        the honest answer in the file a reader cites is no.
        """
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        await self._two_rounds(store, clock, failing, lose_in_parent={"alpha"})

        summary = json.loads((store.round_dir(1) / "metrics.json").read_text())
        assert summary["comparable_to_parent"] is False
        assert [c for c in summary["cells"] if "marginal_gain" in c] == []

    async def test_a_parent_with_no_gate_at_all_is_refused_rather_than_trusted(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Silence is unknown, not fine.

        A record written before the gate existed says nothing about whether it
        measured its whole corpus. Trusting that silence re-opens exactly the
        phantom gain above -- emitted with every gate green, which is the
        property that makes it dangerous -- while refusing it costs a delta
        that comes back the moment the baseline is re-driven under a runner
        that writes the gate.
        """
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        _, second = await self._two_rounds(
            store, clock, failing, lose_in_parent=set(), strip_parent_gate=True
        )

        assert second.record.deltas == ()
        assert {a.verdict for a in second.assessments} == {
            SaturationVerdict.REFUSED_PARENT_ATTEMPT_LOST
        }
        assert "records no all_attempts_completed gate" in second.record.verdict
        summary = json.loads((store.round_dir(1) / "metrics.json").read_text())
        assert summary["comparable_to_parent"] is False

    async def test_two_lossy_rounds_name_this_round_first_and_the_parent_too(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Both sides short a problem: the verdict has to survive that.

        This round's own loss is the verdict -- its gate and its verdict
        prefix already say so, and a verdict naming only the parent would
        contradict them -- but the parent's loss is named in the same reason,
        so nobody re-drives this round expecting the delta back.
        """
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        _, second = await self._two_rounds(
            store, clock, failing, lose_in_parent={"alpha"}, lose_in_child={"beta"}
        )

        assert second.record.deltas == ()
        assert {a.verdict for a in second.assessments} == {SaturationVerdict.REFUSED_ATTEMPT_LOST}
        assert second.record.gates["all_attempts_completed"] is False
        assert second.record.verdict.startswith("1 of 2 attempt(s) failed")
        assert "the parent round is also not a full-corpus measurement" in second.record.verdict

    async def test_a_complete_parent_still_gets_its_delta(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The negative half: the clean path is unchanged, byte for byte.

        Same corpus, same floor, same lineage, nothing lost on either side.
        The delta, the saturation call and both ``comparable_to_parent``
        fields must be exactly what they were before the parent-side refusal
        existed, or the refusal has bought honesty by breaking the measurement
        it protects.
        """
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        _, second = await self._two_rounds(store, clock, failing, lose_in_parent=set())

        assert len(second.record.deltas) == 1
        assert second.record.deltas[0].marginal_gain == pytest.approx(0.0)
        assert {a.verdict for a in second.assessments} == {SaturationVerdict.SATURATED}
        summary = json.loads((store.round_dir(1) / "metrics.json").read_text())
        assert summary["comparable_to_parent"] is True
        row = json.loads(store.trajectory_path.read_text())["rounds"][1]
        assert row["saturation"][0]["verdict"] == "saturated"
        assert row["delta"] != {}


# --------------------------------------------------------------------------- #
# Round-7 regression -- a namespaced problem id must not vanish from the viewer
# --------------------------------------------------------------------------- #


class TestNestedProblemIdsStayInTheViewerList:
    """``problem.id`` is an unvalidated path component (RES-16).

    An operator namespacing a corpus -- ``"cuda/matmul-speedup"`` -- gets an
    attempt directory two segments below ``attempts/``, and the single-segment
    glob that replaced ``.viewer.json``'s f-string construction matched
    exactly one. The run executed, chained and verified perfectly; it simply
    stopped being listed, with no log line and no warning. ``verify``'s walk
    has always been recursive, so the two tools disagreed about which runs
    existed on disk -- and the silent one was the one the desktop reads.
    """

    async def test_a_namespaced_id_is_listed_and_matches_what_verify_finds(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_round(
            [
                make_problem("cuda/matmul-speedup", scores=(2.0,)),
                make_problem("flat", scores=(1.0,)),
            ],
            make_config(),
        )
        results_root = store.loop_dir.parent
        viewer = json.loads((results_root / ".viewer.json").read_text())

        assert "loop-test-loop/round-00/attempts/cuda/matmul-speedup" in viewer["runs"]
        assert "loop-test-loop/round-00/attempts/flat" in viewer["runs"]
        # The two tools must agree about what is on disk. ``find_runs`` is the
        # depth-independent walk that always found this run.
        assert sorted(viewer["runs"]) == sorted(
            p.relative_to(results_root).as_posix() for p in verify_cli.find_runs(results_root)
        )

    async def test_a_rotated_prior_chain_under_a_nested_id_is_still_excluded(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Depth no longer separates a live run from a superseded one.

        Rotation puts ``prior-N/`` one segment below the attempt directory,
        which under a flat id was two segments below ``attempts/`` -- exactly
        where a nested id's live run also lives. The exclusion therefore moved
        from depth to directory name, and this is the test that the move did
        not quietly re-admit a superseded chain to the viewer while a nested
        live one is listed.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        config = make_config()
        await runner.run_round([make_problem("cuda/matmul", scores=(1.0,))], config)
        # A second round-0 drive over the same directory rotates the first.
        await runner.run_attempt(
            make_problem("cuda/matmul", scores=(2.0,)), config, output_dir=store.round_dir(0)
        )
        await runner.run_round(
            [make_problem("cuda/matmul", scores=(2.0,))],
            make_config(round_index=1, run_id="r01", parent_round_id="r00"),
        )

        metrics_dir = store.round_dir(0) / "attempts" / "cuda" / "matmul"
        assert (metrics_dir / "prior-1" / "metrics.jsonl").is_file()
        viewer = json.loads((store.loop_dir.parent / ".viewer.json").read_text())
        assert viewer["runs"] == [
            "loop-test-loop/round-00/attempts/cuda/matmul",
            "loop-test-loop/round-01/attempts/cuda/matmul",
        ]

    async def test_a_flat_id_is_listed_exactly_as_before(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The un-nested path is the one that already worked; widening the walk
        must not have changed it.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_round([make_problem("s1"), make_problem("s2")], make_config())
        viewer = json.loads((store.loop_dir.parent / ".viewer.json").read_text())
        assert viewer["runs"] == [
            "loop-test-loop/round-00/attempts/s1",
            "loop-test-loop/round-00/attempts/s2",
        ]


# --------------------------------------------------------------------------- #
# Round-7 regression -- a finished round must not report "still running"
# --------------------------------------------------------------------------- #


#: Three steps, escalating on the second. With ``verify_every_step=False`` the
#: first two steps append plain metrics lines (no raw-score key, so a
#: colliding ``score_scale`` cannot bite yet) and the third trips the cap,
#: which forces the one verification -- and *that* append carries the score
#: and raises. The attempt therefore dies with lines already on disk, having
#: interrupted its operator once on the way.
_ESCALATING_SCRIPT = (
    SolverStep(tokens=10, note="work"),
    SolverStep(tokens=10, note="stuck", escalate=EscalationReason.HARNESS_FAILURE),
    SolverStep(tokens=10, note="work"),
)


class TestAFinishedRoundThatLostAnAttemptExitsZero:
    """``INCOMPLETE`` must mean unfinished, not "finished badly".

    A contained crash *after* the first metrics append left an intact chain
    with no ``metrics.json`` beside it -- ``verify``'s definition of
    ``INCOMPLETE`` -- on a round that was over. The whole results root then
    exited ``2`` permanently: a pre-writeup gate demanding ``0`` could never
    pass on that tree, and an operator taught "``2`` means still running"
    would read ``2`` where nothing would ever finish. That is a new
    cry-wolf, which is the failure class this contract exists to remove.

    A contained crash *before* the first append -- the ``score_scale``
    collision the containment fix was built around -- left no ``metrics.jsonl``
    at all, so the directory was not a run and the root exited ``0``. Same
    event class, two different codes, decided by which line of ``run_attempt``
    raised. The exit-code contract is unchanged; what changed is that a
    finished round now finishes its own records.
    """

    @staticmethod
    def _colliding(problem_id: str, scale: str) -> Problem:
        problem = make_problem(problem_id)
        return dataclasses.replace(
            problem, verifier=dataclasses.replace(problem.verifier, score_scale=scale)
        )

    async def _round_losing_one_attempt_after_its_first_line(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        *,
        solver: object | None = None,
        bad_first: bool = False,
    ) -> Path:
        runner = make_runner(
            solver=solver or FakeSolver(),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=ScriptedEscalationChannel([EscalationVerdict.CONTINUE]),
        )
        # ``FakeSolver`` walks one script across the whole round, so a test
        # that needs the *lost* problem to escalate has to run it first.
        good = make_problem("good", scores=(2.0,))
        bad = self._colliding("bad", "tokens_used")
        await runner.run_round(
            [bad, good] if bad_first else [good, bad],
            make_config(
                verify_every_step=False,
                cap=Cap(max_steps=3, max_tokens=10_000, max_wall_clock_seconds=600.0),
            ),
        )
        return store.round_dir(0) / "attempts" / "bad"

    async def test_a_crash_after_the_first_line_leaves_a_terminal_summary_and_exits_zero(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        metrics_dir = await self._round_losing_one_attempt_after_its_first_line(
            store, workspaces, clock
        )
        # The precondition the defect needed: lines on disk, chain intact.
        assert len(_read_lines(metrics_dir / "metrics.jsonl")) >= 1
        assert (await verify_metrics_chain(metrics_dir)).ok is True

        summary = json.loads((metrics_dir / "metrics.json").read_text())
        assert summary["best_score"] is None
        assert summary["best_passed_correctness"] is None
        assert summary["final_state"].startswith("crashed_in_harness:")
        assert "ContractViolationError" in summary["final_state"]
        # Not an AttemptState value: a harness crash is not an outcome the
        # experiment measured, and must not be readable as one.
        assert summary["final_state"] not in {s.value for s in AttemptState}

        assert await _verify_exit_code(store.loop_dir.parent) == verify_cli.EXIT_OK

    async def test_the_terminal_summary_reconciles_with_the_log_it_was_derived_from(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Trading ``INCOMPLETE`` for ``FAILED`` would be strictly worse.

        ``FAILED`` is this tool's word for a doctored record. A summary
        written on the error path from anything other than the log's own last
        line would earn exactly that verdict on an attempt whose only sin was
        crashing, so every field is re-derived from the log by the same reader
        ``reconcile_summary`` uses.
        """
        metrics_dir = await self._round_losing_one_attempt_after_its_first_line(
            store, workspaces, clock
        )
        verdict = await reconcile_summary(metrics_dir)
        assert verdict.state is ReconcileState.OK, verdict.reason
        assert verdict.mismatches == ()

        lines = _read_lines(metrics_dir / "metrics.jsonl")
        summary = json.loads((metrics_dir / "metrics.json").read_text())
        assert summary["steps_recorded"] == len(lines)
        assert summary["outcome"] == lines[-1]["outcome_code"]
        assert summary["consumed_steps"] == lines[-1]["consumed_steps"]
        assert summary["consumed_tokens"] == lines[-1]["tokens_used"]
        assert summary["cap_extensions"] == lines[-1]["cap_extensions"]

    async def test_escalations_the_lost_attempt_raised_are_counted_not_zeroed(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The in-memory request list died with the attempt; the files did not.

        Writing ``0`` here would put a fabricated number in a file whose whole
        purpose is to be citable -- and it would understate driving function
        #4, human-gate load, which is the number the self-improvement claim
        lives or dies on. The requests were written to disk before the loop
        suspended on each one, so the real count is recoverable.
        """
        metrics_dir = await self._round_losing_one_attempt_after_its_first_line(
            store, workspaces, clock, solver=FakeSolver(_ESCALATING_SCRIPT), bad_first=True
        )
        escalations = sorted((store.round_dir(0) / "escalations").glob("*.json"))
        raised_by_bad = [
            path
            for path in escalations
            if json.loads(path.read_text())["request"]["problem_id"] == "bad"
        ]
        assert raised_by_bad, "the scripted solver did not actually escalate"
        summary = json.loads((metrics_dir / "metrics.json").read_text())
        assert summary["escalation_count"] == len(raised_by_bad)

    async def test_a_crash_before_the_first_line_still_exits_zero(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The other half of the same event class, and it must agree.

        With per-step verification the colliding scale raises at the *first*
        append, so nothing is written and the directory is not a run at all.
        Nothing is fabricated to make it one -- writing a summary for a run
        that does not exist would create a finding rather than clear one.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_round(
            [make_problem("good", scores=(2.0,)), self._colliding("bad", "progress")],
            make_config(),
        )
        assert not (store.round_dir(0) / "attempts" / "bad" / "metrics.jsonl").exists()
        assert await _verify_exit_code(store.loop_dir.parent) == verify_cli.EXIT_OK

    async def test_a_genuinely_unfinished_run_still_exits_two(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The negative half, and the reason the exit-code contract is unchanged.

        ``2`` still has to fire for the case it was introduced for: an attempt
        killed mid-run -- a closed subscription window, a killed process --
        which reaches no containment path at all because the whole process is
        gone. Its intact chain with no summary is what ``INCOMPLETE`` means,
        and this fix must not have made that state unreachable.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_round([make_problem("s1", scores=(2.0,))], make_config())
        (store.round_dir(0) / "attempts" / "s1" / "metrics.json").unlink()

        assert await _verify_exit_code(store.loop_dir.parent) == verify_cli.EXIT_INCOMPLETE

    async def test_a_summary_already_on_disk_is_never_overwritten(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """A crash *after* the summary landed leaves the honest record alone.

        The terminal write is a floor under the reporting contract, not a
        replacement for it: the real summary carries the attempt's score and
        state, and clobbering it with the cruder crashed-in-harness record
        would lose information the run genuinely produced.
        """
        output_dir = store.round_dir(0)
        metrics_dir = output_dir / "attempts" / "s1"
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await runner.run_attempt(
            make_problem("s1", scores=(2.0,)), make_config(), output_dir=output_dir
        )
        honest = (metrics_dir / "metrics.json").read_bytes()

        await runner._close_out_crashed_attempt(
            problem=make_problem("s1", scores=(2.0,)),
            config=make_config(),
            output_dir=output_dir,
            exc_type="RuntimeError",
        )
        assert (metrics_dir / "metrics.json").read_bytes() == honest


# --------------------------------------------------------------------------- #
# Round-8 regression -- a close-out must not speak for a prior generation
# --------------------------------------------------------------------------- #


class TestACloseOutNeverWritesOverAnotherGenerationsChain:
    """``_close_out_crashed_attempt``'s docstring promised this guard; the code
    did not implement it.

    ``_read_crashed_attempt_record`` only checked that the header *had* an
    ``attempt_id``, and the close-out only checked "summary absent, jsonl
    present" -- so the identity of the chain it adopted was never compared
    with the attempt that actually crashed. The shape is reachable, not
    theoretical: ``run_attempt`` materialises the workspace *before* it
    rotates the stale trio aside, so an exception in that window (a pruned
    template, an evicted volume, a full disk) leaves the previous
    generation's chain exactly where it was -- intact, summary-less, the
    honest "killed before its summary write" state ``verify`` calls
    ``INCOMPLETE`` and rotation exists to preserve.

    Driven, that produced a ``metrics.json`` asserting generation 1's
    ``attempt_id`` beside generation 2's ``round_id``, generation 2's ``seed``
    and ``crashed_in_harness:OSError`` -- an exception that never touched that
    log -- and ``verify`` reported ``OK`` with zero mismatches, because every
    field it reconciles had just been re-derived from the very log the summary
    was misattributed to. Two harms, either sufficient: a fabricated claim
    about a run that did not make it, and the destruction of the one state
    that said the earlier attempt was killed unfinished.
    """

    @staticmethod
    def _corpus() -> list[Problem]:
        return [make_problem("alpha", scores=(3.0,)), make_problem("beta", scores=(0.5,))]

    async def _generation_one_killed_before_its_summary(
        self,
        store: TrajectoryStore,
        workspaces: _WorkspacesThatFailForSomeProblems,
        clock: FakeClock,
    ) -> tuple[RoundRunner, Path, dict[str, object]]:
        """A real gen-1 round, with ``beta``'s summary removed after the fact.

        Removing the summary (rather than building the shape by hand) is how
        a killed process actually leaves the directory: the chain is complete
        because every append landed, and ``metrics.json`` is missing because
        the process died in the gap between the last append and the single
        end-of-attempt write that ``run_attempt``'s own docstring names.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        output_dir = store.round_dir(0)
        await runner.run_attempts(
            self._corpus(), make_config(run_id="gen1", seed=7), output_dir=output_dir
        )
        metrics_dir = output_dir / "attempts" / "beta"
        header = json.loads((metrics_dir / "metrics.chain.json").read_text())["header"]
        (metrics_dir / "metrics.json").unlink()
        assert (await verify_cli.verify_run(metrics_dir)).state is verify_cli.RunState.INCOMPLETE
        return runner, metrics_dir, header

    async def test_a_redrive_that_crashes_before_rotation_leaves_the_older_chain_alone(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        runner, metrics_dir, header = await self._generation_one_killed_before_its_summary(
            store, failing, clock
        )
        chain_before = (metrics_dir / "metrics.jsonl").read_bytes()

        failing.fail = {"beta"}
        with structlog.testing.capture_logs() as cap:
            await runner.run_attempts(
                self._corpus(),
                make_config(run_id="gen2", seed=99),
                output_dir=store.round_dir(0),
            )

        # Nothing was written over generation 1's log, and generation 1's log
        # is byte-for-byte what it was.
        assert not (metrics_dir / "metrics.json").exists()
        assert (metrics_dir / "metrics.jsonl").read_bytes() == chain_before
        assert json.loads((metrics_dir / "metrics.chain.json").read_text())["header"] == header

        # The refusal is disclosed, and under its own event name: an operator
        # greps ``..._summary_failed`` when the reporting layer is broken, and
        # this is the opposite -- reporting working correctly.
        refusals = [
            e for e in cap if e.get("event") == "research.results.crashed_attempt_summary_refused"
        ]
        assert len(refusals) == 1
        assert refusals[0]["log_level"] == "error"
        assert "round_id='gen1' (expected 'gen2')" in refusals[0]["reason"]
        assert "seed=7 (expected 99)" in refusals[0]["reason"]
        assert not any(
            e.get("event") == "research.results.crashed_attempt_summary_failed" for e in cap
        )

    async def test_the_killed_generations_honest_incomplete_state_survives(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """``INCOMPLETE`` is the true statement about that directory.

        The attempt was killed unfinished and never rotated aside, so "the
        summary is missing" is exactly what happened. Trading it for an ``OK``
        earned by a fabricated summary is strictly worse than the ``2`` an
        operator has to go look at: the ``2`` is a finding, the ``OK`` is a
        false negative that also erased the evidence.
        """
        failing = _WorkspacesThatFailForSomeProblems(workspaces.root)
        runner, metrics_dir, _ = await self._generation_one_killed_before_its_summary(
            store, failing, clock
        )
        failing.fail = {"beta"}
        await runner.run_attempts(
            self._corpus(), make_config(run_id="gen2", seed=99), output_dir=store.round_dir(0)
        )

        assert (await verify_metrics_chain(metrics_dir)).ok is True
        verdict = await verify_cli.verify_run(metrics_dir)
        assert verdict.state is verify_cli.RunState.INCOMPLETE
        assert await _verify_exit_code(store.loop_dir.parent) == verify_cli.EXIT_INCOMPLETE

    async def test_a_crash_after_rotation_still_gets_its_own_terminal_summary(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The guard has to be precise, not merely safe.

        Here the re-drive gets far enough to rotate the previous generation
        aside and start its own chain, and *then* dies (a colliding
        ``score_scale`` on the first append that carries a score). The header
        in the directory is now this attempt's, so the close-out writes the
        terminal summary it exists to write -- and generation 1's complete
        pair sits untouched in ``prior-1/``. A guard that refused here would
        have re-introduced the permanent exit ``2`` the close-out removed.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        output_dir = store.round_dir(0)
        config = make_config(
            run_id="gen1",
            seed=7,
            verify_every_step=False,
            cap=Cap(max_steps=3, max_tokens=10_000, max_wall_clock_seconds=600.0),
        )
        await runner.run_attempts(
            [make_problem("bad", scores=(2.0,))], config, output_dir=output_dir
        )
        metrics_dir = output_dir / "attempts" / "bad"
        first_generation = (metrics_dir / "metrics.json").read_bytes()

        await runner.run_attempts(
            # A surviving sibling, because a round that loses *every* attempt
            # is refused outright one level up and never reaches a close-out.
            [_problem_with_scale("bad", "tokens_used"), make_problem("good", scores=(1.0,))],
            dataclasses.replace(config, run_id="gen2", seed=99),
            output_dir=output_dir,
        )

        summary = json.loads((metrics_dir / "metrics.json").read_text())
        header = json.loads((metrics_dir / "metrics.chain.json").read_text())["header"]
        assert summary["round_id"] == header["round_id"] == "gen2"
        assert summary["seed"] == header["seed"] == 99
        assert summary["attempt_id"] == header["attempt_id"]
        assert summary["final_state"].startswith("crashed_in_harness:")
        assert (await reconcile_summary(metrics_dir)).state is ReconcileState.OK

        # Generation 1 survives, whole, where rotation put it.
        assert (metrics_dir / "prior-1" / "metrics.json").read_bytes() == first_generation
        assert (await reconcile_summary(metrics_dir / "prior-1")).state is ReconcileState.OK
        assert await _verify_exit_code(store.loop_dir.parent) == verify_cli.EXIT_OK
