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

from turing.research.contracts import AttemptState, ContractViolationError, EscalationVerdict
from turing.research.loop import plots as plots_module
from turing.research.loop import verify as verify_cli
from turing.research.loop.integrity import CHAIN_FIELD, reconcile_summary, verify_metrics_chain
from turing.research.loop.results import Outcome
from turing.research.loop.runner import PassCriterion

from .conftest import (
    DEFAULT_CAP,
    ExplodingSolver,
    FakeSolver,
    ScriptedEscalationChannel,
    make_config,
    make_problem,
    make_runner,
)

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import Attempt, Problem
    from turing.research.loop.protocols import SolverTask
    from turing.research.loop.trajectory import TrajectoryStore

    from .conftest import FakeClock, TempWorkspaceProvider


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
