"""The round loop end to end with a FakeEngine, a real verifier command, and a real git sandbox."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest

from turing.research.contracts import ContractViolationError, FrozenVerifierError
from turing.research.rsi.cheat import CheatDetector, git_env, run_git
from turing.research.rsi.contracts import (
    VERIFIER_LOCK_FILENAME,
    EngineResult,
    RoundRecord,
    RsiConfig,
    SelfEditInputs,
    VerifierSpec,
    sha256_text,
)
from turing.research.rsi.engine import FakeEngine, ok_result
from turing.research.rsi.loop import (
    DEFAULT_SCAFFOLD,
    EXIT_ENGINE_FAILURE,
    SCAFFOLD_FILENAME,
    LoopOutcome,
    RsiLoop,
    StopReason,
    append_jsonl,
    build_round_prompt,
    describe_plan,
    read_trajectory,
    redact,
)
from turing.research.rsi.taxonomy import TAXONOMY_DIGEST, FailureCategory

from .conftest import RsiDirs, bash_round

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

VERIFY_SH = "cat SCORE 2>/dev/null || exit 1\n"
VERIFIER = VerifierSpec(command="sh verify.sh", files=("verify.sh",))
PASS_FAIL_VERIFIER = VerifierSpec(command="test -f PASS")
MIXED_VERIFY_SH = (
    "if [ -f SCORE ]; then\n"
    "    cat SCORE\n"
    "elif [ -f PASS ]; then\n"
    "    exit 0\n"
    "else\n"
    "    exit 1\n"
    "fi\n"
)
MIXED_VERIFIER = VerifierSpec(command="sh verify.sh", files=("verify.sh",))
_ROUND_RE = re.compile(r"You are round (\d+) of")


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0, step: float = 10.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _round_of(prompt: str) -> int:
    m = _ROUND_RE.search(prompt)
    assert m is not None, prompt[:200]
    return int(m.group(1))


def score_step(
    value: float | None,
    *,
    metrics: bool = True,
    report: float | str | None = "same",
    results: Path,
    extra: Callable[[Path], None] | None = None,
) -> Callable[[str, Path], EngineResult]:
    """A scripted round: write SCORE (or delete it), append a metrics line, run ``extra``."""

    def step(prompt: str, cwd: Path) -> EngineResult:
        round_no = _round_of(prompt)
        score_file = cwd / "SCORE"
        if value is None:
            score_file.unlink(missing_ok=True)
        else:
            score_file.write_text(f"score={value}\n")
        if metrics:
            line: dict[str, Any] = {"step": round_no, "ts": 1}
            reported = value if report == "same" else report
            if reported is not None:
                line["score"] = reported
            append_jsonl(results / "metrics.jsonl", json.dumps(line))
        if extra is not None:
            extra(cwd)
        return ok_result()

    return step


def pass_fail_step(
    *,
    results: Path,
    verifier_passes: bool = True,
    metrics: bool = True,
    engine_exit: int = 0,
) -> Callable[[str, Path], EngineResult]:
    """A real scoreless verifier round with optional engine/telemetry failures."""

    def step(prompt: str, cwd: Path) -> EngineResult:
        round_no = _round_of(prompt)
        marker = cwd / "PASS"
        (cwd / "SCORE").unlink(missing_ok=True)
        if verifier_passes:
            marker.write_text("valid\n")
        else:
            marker.unlink(missing_ok=True)
        if metrics:
            append_jsonl(
                results / "metrics.jsonl",
                json.dumps({"step": round_no, "ts": 1, "measurement": 1.0}),
            )
        return EngineResult(
            exit_code=engine_exit,
            stdout="",
            stderr="",
            wall_seconds=0.01,
            timed_out=False,
        )

    return step


def _config(dirs: RsiDirs, **overrides: Any) -> RsiConfig:
    return replace(dirs.config, **overrides)


def _loop(
    dirs: RsiDirs,
    engine: FakeEngine,
    *,
    config: RsiConfig | None = None,
    self_edit: Any = None,
    verifier: VerifierSpec | None = VERIFIER,
    problem: str | None = "Make the number bigger.",
    verifier_script: str = VERIFY_SH,
) -> RsiLoop:
    (dirs.sandbox / "verify.sh").write_text(verifier_script)
    return RsiLoop(
        config or dirs.config,
        engine=engine,
        verifier=verifier,
        self_edit=self_edit,
        cheat=CheatDetector(),
        clock=FakeClock(),
        problem=problem,
    )


def _lines(results: Path) -> list[dict[str, Any]]:
    path = results / "trajectory.json"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _records(results: Path) -> list[dict[str, Any]]:
    return [line for line in _lines(results) if "event" not in line]


#: Provenance lines every run appends; the self-edit tests look past them.
_BOOKKEEPING_EVENTS = {"verifier_locked", "scaffold_seeded"}


def _all_events(results: Path) -> list[dict[str, Any]]:
    return [line for line in _lines(results) if "event" in line]


def _events(results: Path) -> list[dict[str, Any]]:
    return [e for e in _all_events(results) if e["event"] not in _BOOKKEEPING_EVENTS]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class TestHelpers:
    def test_append_jsonl_appends_and_refuses_multiline(self, results: Path) -> None:
        path = results / "t.jsonl"
        append_jsonl(path, '{"a": 1}')
        append_jsonl(path, '{"b": 2}\n')
        assert path.read_text() == '{"a": 1}\n{"b": 2}\n'
        with pytest.raises(ContractViolationError):
            append_jsonl(path, "{}\n{}")

    def test_read_trajectory_skips_events_and_loads_bash_lines(
        self, results: Path, write_trajectory: Callable[[Sequence[dict[str, Any]]], Path]
    ) -> None:
        write_trajectory(
            [
                bash_round(1),
                {"event": "self_edit", "round": 1, "ts": 5, "scaffold_sha": "abc"},
                bash_round(2, exit_code=1),
                RoundRecord(round=3, started=1, ended=2, exit=0, score=2.0, passed=True).to_json(),
            ]
        )
        state = read_trajectory(results / "trajectory.json")
        assert state.next_round == 4
        assert [r.round for r in state.records] == [1, 2, 3]
        assert [e.event for e in state.events] == ["self_edit"]
        assert state.best_score == 2.0
        assert state.previous_score == 2.0

    def test_read_trajectory_refuses_corruption(self, results: Path) -> None:
        (results / "trajectory.json").write_text('{"round": 1}\nnot json\n')
        with pytest.raises(ContractViolationError):
            read_trajectory(results / "trajectory.json")

    def test_redact_removes_needles_longest_first(self) -> None:
        assert redact("run sh verify.sh now sh", ["sh", "sh verify.sh"]) == (
            "run <redacted> now <redacted>"
        )
        assert redact("plain", ["", "zzz"]) == "plain"

    def test_prompt_prepends_scaffold_and_keeps_bash_contract(self, results: Path) -> None:
        prompt = build_round_prompt(
            round_no=7, results_dir=results, scaffold_text="RULE ONE\n", verifier_command="sh v.sh"
        )
        assert prompt.startswith("RULE ONE\n\nYou are round 7 of a continuous research loop.")
        assert (
            f'{results}/metrics.jsonl of the form {{"step": 7, "ts": <epoch seconds>, ...}}'
            in prompt
        )
        assert "create an empty file named STOP" in prompt
        assert "commit your work with\ngit" in prompt
        assert "    sh v.sh\n" in prompt
        assert "the LOOP measures the score" in prompt
        assert VERIFIER_LOCK_FILENAME in prompt


# --------------------------------------------------------------------------- #
# Rounds
# --------------------------------------------------------------------------- #


class TestRounds:
    async def test_three_rounds_scores_1_2_2(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(
            script=[
                score_step(1, results=results),
                score_step(2, results=results),
                score_step(2, results=results),
            ]
        )
        loop = _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=3))
        outcome = await loop.run()
        assert isinstance(outcome, LoopOutcome)
        assert outcome.stop_reason is StopReason.ROUNDS_EXHAUSTED
        assert outcome.exit_code == 0
        assert outcome.rounds_run == 3
        assert outcome.best_score == 2.0

        recs = _records(results)
        assert [r["categories"] for r in recs] == [[], [], ["no_progress"]]
        assert [r["score"] for r in recs] == [1.0, 2.0, 2.0]
        assert all(r["passed"] and not r["void"] for r in recs)
        assert [r["agent_reported_score"] for r in recs] == [1.0, 2.0, 2.0]
        # I7: bash keys first, in the bash order.
        assert list(recs[0])[:4] == ["round", "started", "ended", "exit"]
        assert recs[0]["ended"] > recs[0]["started"]
        assert recs[0]["exit"] == 0
        assert recs[0]["verifier_wall_seconds"] is not None

        # First-run side effects, same layout as the bash script.
        assert (rsi_dirs.sandbox / "PROBLEM.md").read_text() == "Make the number bigger.\n"
        assert (rsi_dirs.sandbox / "NOTES.md").exists()
        assert (rsi_dirs.sandbox / ".git").is_dir()
        assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD
        assert (rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).exists()
        assert json.loads((results / "taxonomy.json").read_text())["digest"] == TAXONOMY_DIGEST
        tracked = await run_git(rsi_dirs.sandbox, "ls-files", check=True)
        assert VERIFIER_LOCK_FILENAME in tracked.stdout.split()

        # The prompt: scaffold first, then the bash contract, then the verifier command line.
        prompts = [c.prompt for c in engine.calls]
        assert all(p.startswith(DEFAULT_SCAFFOLD.rstrip("\n")) for p in prompts)
        assert [_round_of(p) for p in prompts] == [1, 2, 3]
        assert "    sh verify.sh\n" in prompts[0]
        assert VERIFY_SH.strip() not in prompts[0]  # never the verifier's internals
        assert engine.calls[0].cwd == rsi_dirs.sandbox
        assert engine.calls[0].timeout_seconds == rsi_dirs.config.round_timeout_seconds

    async def test_scoreless_passes_record_progress_then_no_progress(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(
            script=[
                pass_fail_step(results=results),
                pass_fail_step(results=results),
                pass_fail_step(results=results),
            ]
        )
        outcome = await _loop(
            rsi_dirs,
            engine,
            config=_config(rsi_dirs, rounds=3),
            verifier=PASS_FAIL_VERIFIER,
        ).run()
        assert outcome.best_score is None
        records = _records(results)
        assert [record["categories"] for record in records] == [
            [],
            ["no_progress"],
            ["no_progress"],
        ]
        assert all(record["passed"] and not record["void"] for record in records)
        # The verifier passes with a measured result, and every round has
        # telemetry; no_metrics cannot explain the later categories.
        assert all(record["score"] is None for record in records)
        assert all(record["categories"] != ["no_metrics"] for record in records)

    @pytest.mark.parametrize(
        ("verifier_passes", "expected_categories"),
        [
            ([False, True, True], [["verifier_failed"], [], ["no_progress"]]),
            ([True, False, True], [[], ["verifier_failed"], ["no_progress"]]),
        ],
    )
    async def test_scoreless_failures_do_not_establish_or_clear_prior_pass(
        self,
        rsi_dirs: RsiDirs,
        verifier_passes: list[bool],
        expected_categories: list[list[str]],
    ) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(
            script=[
                pass_fail_step(results=results, verifier_passes=passes)
                for passes in verifier_passes
            ]
        )
        await _loop(
            rsi_dirs,
            engine,
            config=_config(rsi_dirs, rounds=3),
            verifier=PASS_FAIL_VERIFIER,
        ).run()
        categories = [record["categories"] for record in _records(results)]
        assert categories == expected_categories

    @pytest.mark.parametrize(
        ("engine_exit", "metrics", "prior_category"),
        [(1, True, "engine_error"), (0, False, "no_metrics"), (1, False, None)],
    )
    async def test_mixed_engine_and_metrics_flags_on_a_pass_still_establish_prior_pass(
        self,
        rsi_dirs: RsiDirs,
        engine_exit: int,
        metrics: bool,
        prior_category: str | None,
    ) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(
            script=[
                pass_fail_step(results=results, engine_exit=engine_exit, metrics=metrics),
                pass_fail_step(results=results),
            ]
        )
        await _loop(
            rsi_dirs,
            engine,
            config=_config(rsi_dirs, rounds=2),
            verifier=PASS_FAIL_VERIFIER,
        ).run()
        records = _records(results)
        assert records[0]["passed"] is True and records[0]["void"] is False
        if prior_category is None:
            assert set(records[0]["categories"]) == {"engine_error", "no_metrics"}
        else:
            assert records[0]["categories"] == [prior_category]
        assert records[1]["categories"] == ["no_progress"]

    @pytest.mark.parametrize("numeric_score", [0.0, -1.0])
    async def test_live_numeric_zero_or_negative_then_scoreless_is_no_progress(
        self, rsi_dirs: RsiDirs, numeric_score: float
    ) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(
            script=[
                score_step(numeric_score, results=results),
                pass_fail_step(results=results),
            ]
        )
        outcome = await _loop(
            rsi_dirs,
            engine,
            config=_config(rsi_dirs, rounds=2),
            verifier=MIXED_VERIFIER,
            verifier_script=MIXED_VERIFY_SH,
        ).run()
        records = _records(results)
        assert records[0]["score"] == numeric_score
        assert records[1]["score"] is None
        assert records[1]["categories"] == ["no_progress"]
        assert outcome.best_score == numeric_score

    async def test_live_scoreless_then_negative_numeric_has_no_progress_or_regressed(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(
            script=[
                pass_fail_step(results=results),
                score_step(-1.0, results=results),
            ]
        )
        outcome = await _loop(
            rsi_dirs,
            engine,
            config=_config(rsi_dirs, rounds=2),
            verifier=MIXED_VERIFIER,
            verifier_script=MIXED_VERIFY_SH,
        ).run()
        records = _records(results)
        assert records[0]["score"] is None and records[0]["categories"] == []
        assert records[1]["score"] == -1.0
        assert records[1]["categories"] == []
        assert outcome.best_score == -1.0

    async def test_live_numeric_then_scoreless_then_lower_numeric_preserves_best_without_regression(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(
            script=[
                score_step(5.0, results=results),
                pass_fail_step(results=results),
                score_step(4.0, results=results),
            ]
        )
        outcome = await _loop(
            rsi_dirs,
            engine,
            config=_config(rsi_dirs, rounds=3),
            verifier=MIXED_VERIFIER,
            verifier_script=MIXED_VERIFY_SH,
        ).run()
        records = _records(results)
        assert [record["categories"] for record in records] == [
            [],
            ["no_progress"],
            ["no_progress"],
        ]
        assert outcome.best_score == 5.0

    @pytest.mark.parametrize("numeric_score", [0.0, -1.0])
    async def test_fresh_loop_restart_numeric_then_scoreless_keeps_prior_pass(
        self, rsi_dirs: RsiDirs, numeric_score: float
    ) -> None:
        results = rsi_dirs.results
        first = await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(numeric_score, results=results)]),
            config=_config(rsi_dirs, rounds=1),
            verifier=MIXED_VERIFIER,
            verifier_script=MIXED_VERIFY_SH,
        ).run()
        second = await _loop(
            rsi_dirs,
            FakeEngine(script=[pass_fail_step(results=results)]),
            config=_config(rsi_dirs, rounds=1),
            verifier=MIXED_VERIFIER,
            verifier_script=MIXED_VERIFY_SH,
        ).run()
        assert first.best_score == numeric_score
        assert second.best_score == numeric_score
        assert _records(results)[1]["categories"] == ["no_progress"]

    async def test_fresh_loop_restart_scoreless_then_negative_numeric_is_progress(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        first = await _loop(
            rsi_dirs,
            FakeEngine(script=[pass_fail_step(results=results)]),
            config=_config(rsi_dirs, rounds=1),
            verifier=MIXED_VERIFIER,
            verifier_script=MIXED_VERIFY_SH,
        ).run()
        second = await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(-1.0, results=results)]),
            config=_config(rsi_dirs, rounds=1),
            verifier=MIXED_VERIFIER,
            verifier_script=MIXED_VERIFY_SH,
        ).run()
        assert first.best_score is None
        assert second.best_score == -1.0
        assert _records(results)[1]["categories"] == []

    async def test_fresh_loop_restart_numeric_scoreless_lower_numeric_uses_numeric_best(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        first = await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(5.0, results=results)]),
            config=_config(rsi_dirs, rounds=1),
            verifier=MIXED_VERIFIER,
            verifier_script=MIXED_VERIFY_SH,
        ).run()
        second = await _loop(
            rsi_dirs,
            FakeEngine(script=[pass_fail_step(results=results)]),
            config=_config(rsi_dirs, rounds=1),
            verifier=MIXED_VERIFIER,
            verifier_script=MIXED_VERIFY_SH,
        ).run()
        third = await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(4.0, results=results)]),
            config=_config(rsi_dirs, rounds=1),
            verifier=MIXED_VERIFIER,
            verifier_script=MIXED_VERIFY_SH,
        ).run()
        assert first.best_score == 5.0
        assert second.best_score == 5.0
        assert third.best_score == 5.0
        records = _records(results)
        assert [record["categories"] for record in records] == [
            [],
            ["no_progress"],
            ["no_progress"],
        ]

    async def test_regression_and_no_progress_both_recorded(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(script=[score_step(3, results=results), score_step(1, results=results)])
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=2)).run()
        assert outcome.exit_code == 0
        assert _records(results)[1]["categories"] == ["no_progress", "regressed"]

    async def test_verifier_failure_is_recorded_not_fatal(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(script=[score_step(None, results=results, report=None)])
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=1)).run()
        assert outcome.stop_reason is StopReason.ROUNDS_EXHAUSTED
        rec = _records(results)[0]
        assert rec["categories"] == ["verifier_failed"]
        assert rec["score"] is None and rec["passed"] is False and rec["void"] is False

    async def test_missing_metrics_line_is_no_metrics_alongside(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(
            script=[
                score_step(1, results=results, metrics=False),
                score_step(1, results=results, metrics=False),
            ]
        )
        await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=2)).run()
        recs = _records(results)
        assert recs[0]["categories"] == ["no_metrics"]
        assert recs[1]["categories"] == ["no_metrics", "no_progress"]
        assert recs[0]["agent_reported_score"] is None

    async def test_metrics_line_for_another_round_does_not_count(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results

        def wrong_step(prompt: str, cwd: Path) -> EngineResult:
            (cwd / "SCORE").write_text("score=1\n")
            append_jsonl(results / "metrics.jsonl", json.dumps({"step": 99, "score": 1}))
            return ok_result()

        await _loop(
            rsi_dirs, FakeEngine(script=[wrong_step]), config=_config(rsi_dirs, rounds=1)
        ).run()
        assert _records(results)[0]["categories"] == ["no_metrics"]

    async def test_timeout_is_recorded_and_counts_as_engine_failure(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        timed_out = EngineResult(
            exit_code=124, stdout="", stderr="", wall_seconds=1800.0, timed_out=True
        )
        engine = FakeEngine(script=[timed_out, score_step(1, results=results)])
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=2)).run()
        assert outcome.stop_reason is StopReason.ROUNDS_EXHAUSTED
        recs = _records(results)
        assert recs[0]["exit"] == 124
        assert set(recs[0]["categories"]) == {"timeout", "verifier_failed", "no_metrics"}
        assert "engine_error" not in recs[0]["categories"]
        assert recs[1]["categories"] == []

    async def test_three_consecutive_engine_failures_abort(self, rsi_dirs: RsiDirs) -> None:
        bad = EngineResult(
            exit_code=1, stdout="", stderr="no claude", wall_seconds=0.1, timed_out=False
        )
        engine = FakeEngine(script=[bad, bad, bad, bad])
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=10)).run()
        assert outcome.stop_reason is StopReason.ENGINE_FAILURES
        assert outcome.exit_code == EXIT_ENGINE_FAILURE
        assert outcome.rounds_run == 3
        assert len(engine.calls) == 3
        assert all("engine_error" in r["categories"] for r in _records(rsi_dirs.results))

    async def test_engine_failure_counter_resets_on_success(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        bad = EngineResult(exit_code=1, stdout="", stderr="", wall_seconds=0.1, timed_out=False)
        engine = FakeEngine(
            script=[
                bad,
                bad,
                score_step(1, results=results),
                bad,
                bad,
                score_step(2, results=results),
            ]
        )
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=6)).run()
        assert outcome.stop_reason is StopReason.ROUNDS_EXHAUSTED
        assert outcome.rounds_run == 6

    async def test_stop_file_is_honoured_before_a_round(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results

        def make_stop(cwd: Path) -> None:
            (cwd / "STOP").touch()

        engine = FakeEngine(
            script=[score_step(1, results=results, extra=make_stop), score_step(2, results=results)]
        )
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=5)).run()
        assert outcome.stop_reason is StopReason.STOP_FILE
        assert outcome.rounds_run == 1
        assert len(engine.calls) == 1

    async def test_preexisting_stop_file_runs_nothing(self, rsi_dirs: RsiDirs) -> None:
        (rsi_dirs.sandbox / "STOP").touch()
        engine = FakeEngine(script=[])
        outcome = await _loop(rsi_dirs, engine).run()
        assert outcome.stop_reason is StopReason.STOP_FILE
        assert outcome.rounds_run == 0

    async def test_unlimited_rounds_until_stop(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results

        def make_stop(cwd: Path) -> None:
            (cwd / "STOP").touch()

        engine = FakeEngine(
            script=[
                score_step(1, results=results),
                score_step(2, results=results),
                score_step(3, results=results, extra=make_stop),
            ]
        )
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=0)).run()
        assert outcome.rounds_run == 3
        assert outcome.stop_reason is StopReason.STOP_FILE


class TestResume:
    async def test_start_round_skips_event_lines_and_carries_best(
        self, rsi_dirs: RsiDirs, write_trajectory: Callable[[Sequence[dict[str, Any]]], Path]
    ) -> None:
        results = rsi_dirs.results
        seed = [
            bash_round(1),
            bash_round(2, exit_code=1),
            {"event": "self_edit", "round": 2, "ts": 1, "scaffold_sha": "deadbeef"},
            RoundRecord(round=3, started=1, ended=2, exit=0, score=5.0, passed=True).to_json(),
            {"event": "rollback", "round": 3, "ts": 2, "reverted": "deadbeef"},
        ]
        write_trajectory(seed)
        # PROBLEM.md already exists: --problem is ignored, PROBLEM.md wins.
        (rsi_dirs.sandbox / "PROBLEM.md").write_text("original\n")
        engine = FakeEngine(script=[score_step(4, results=results), score_step(6, results=results)])
        outcome = await _loop(
            rsi_dirs, engine, config=_config(rsi_dirs, rounds=2), problem="new"
        ).run()
        assert outcome.rounds_run == 2
        assert [_round_of(c.prompt) for c in engine.calls] == [4, 5]
        recs = _records(results)
        assert [r["round"] for r in recs] == [1, 2, 3, 4, 5]
        assert recs[3]["categories"] == ["no_progress", "regressed"]  # 4 < best 5, < previous 5
        assert recs[4]["categories"] == []  # 6 > 5
        assert (rsi_dirs.sandbox / "PROBLEM.md").read_text() == "original\n"
        # I7: append-only — the seed lines are byte-for-byte intact, the run only
        # appended its provenance events and its two round lines.
        lines = _lines(results)
        assert lines[:5] == seed
        assert [e["event"] for e in lines[5:] if "event" in e] == [
            "verifier_locked",
            "scaffold_seeded",
        ]
        assert len(lines) == 9

    @pytest.mark.parametrize(
        ("engine_exit", "metrics", "prior_categories"),
        [
            (0, True, []),
            (1, True, ["engine_error"]),
            (0, False, ["no_metrics"]),
            (1, False, ["engine_error", "no_metrics"]),
        ],
    )
    async def test_scoreless_restart_reconstructs_any_nonvoid_prior_pass(
        self,
        rsi_dirs: RsiDirs,
        engine_exit: int,
        metrics: bool,
        prior_categories: list[str],
    ) -> None:
        results = rsi_dirs.results
        first = await _loop(
            rsi_dirs,
            FakeEngine(
                script=[pass_fail_step(results=results, engine_exit=engine_exit, metrics=metrics)]
            ),
            config=_config(rsi_dirs, rounds=1),
            verifier=PASS_FAIL_VERIFIER,
        ).run()
        assert first.rounds_run == 1
        first_records = _records(results)
        assert first_records[0]["passed"] is True and first_records[0]["void"] is False
        assert first_records[0]["categories"] == prior_categories
        prefix = (results / "trajectory.json").read_bytes()

        second = await _loop(
            rsi_dirs,
            FakeEngine(script=[pass_fail_step(results=results)]),
            config=_config(rsi_dirs, rounds=1),
            verifier=PASS_FAIL_VERIFIER,
        ).run()
        assert second.rounds_run == 1
        assert (results / "trajectory.json").read_bytes().startswith(prefix)
        assert _records(results)[1]["categories"] == ["no_progress"]

    async def test_void_passing_replay_row_does_not_establish_prior_pass(
        self, rsi_dirs: RsiDirs, write_trajectory: Callable[[Sequence[dict[str, Any]]], Path]
    ) -> None:
        write_trajectory(
            [
                RoundRecord(
                    round=1,
                    started=1,
                    ended=2,
                    exit=0,
                    passed=True,
                    void=True,
                    categories=frozenset(
                        {
                            FailureCategory.CHEAT_DETECTED,
                            FailureCategory.ENGINE_ERROR,
                            FailureCategory.NO_METRICS,
                        }
                    ),
                ).to_json()
            ]
        )
        state = read_trajectory(rsi_dirs.results / "trajectory.json")
        assert state.prior_pass is False

    async def test_valid_scoreless_replay_row_establishes_prior_pass(
        self, rsi_dirs: RsiDirs, write_trajectory: Callable[[Sequence[dict[str, Any]]], Path]
    ) -> None:
        write_trajectory(
            [
                RoundRecord(
                    round=1,
                    started=1,
                    ended=2,
                    exit=0,
                    passed=True,
                    categories=frozenset({FailureCategory.NO_METRICS}),
                ).to_json()
            ]
        )
        state = read_trajectory(rsi_dirs.results / "trajectory.json")
        assert state.prior_pass is True

    async def test_first_run_without_problem_is_a_usage_error(self, rsi_dirs: RsiDirs) -> None:
        engine = FakeEngine(script=[])
        with pytest.raises(ContractViolationError, match="--problem is required"):
            await _loop(rsi_dirs, engine, problem=None).run()

    async def test_first_run_without_verifier_is_a_usage_error(self, rsi_dirs: RsiDirs) -> None:
        engine = FakeEngine(script=[])
        with pytest.raises(ContractViolationError, match="--verifier is required"):
            await _loop(rsi_dirs, engine, verifier=None).run()

    async def test_resume_with_different_verifier_is_refused(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(1, results=results)]),
            config=_config(rsi_dirs, rounds=1),
        ).run()
        with pytest.raises(FrozenVerifierError, match="differs from the locked verifier"):
            await _loop(
                rsi_dirs, FakeEngine(script=[]), verifier=VerifierSpec(command="echo score=9")
            ).run()
        assert len(_records(results)) == 1

    async def test_resume_without_verifier_flag_uses_lock(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(1, results=results)]),
            config=_config(rsi_dirs, rounds=1),
        ).run()
        outcome = await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(2, results=results)]),
            config=_config(rsi_dirs, rounds=1),
            verifier=None,
        ).run()
        assert outcome.best_score == 2.0
        assert [r["round"] for r in _records(results)] == [1, 2]

    async def test_taxonomy_digest_mismatch_refuses_to_start(self, rsi_dirs: RsiDirs) -> None:
        (rsi_dirs.results / "taxonomy.json").write_text(
            json.dumps({"version": "0", "digest": "nope"})
        )
        with pytest.raises(ContractViolationError, match="taxonomy digest mismatch"):
            await _loop(rsi_dirs, FakeEngine(script=[])).run()


# --------------------------------------------------------------------------- #
# Cheats and tampering (I1, I2, I5)
# --------------------------------------------------------------------------- #


class TestCheatsStopTheLoop:
    async def test_verifier_tamper_mid_run_voids_round_and_stops(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results

        def tamper(cwd: Path) -> None:
            (cwd / "verify.sh").write_text("echo score=1000\n")

        engine = FakeEngine(
            script=[
                score_step(1, results=results),
                score_step(2, results=results, extra=tamper),
                score_step(3, results=results),
            ]
        )
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=3)).run()
        assert outcome.stop_reason is StopReason.VERIFIER_TAMPERED
        assert outcome.exit_code == 3
        assert outcome.best_score == 1.0
        assert len(engine.calls) == 2  # round 3 never ran
        recs = _records(results)
        assert len(recs) == 2
        assert recs[1]["void"] is True
        assert recs[1]["categories"] == ["verifier_tampered"]
        assert recs[1]["score"] is None  # the tampered verifier was never run
        assert recs[1]["exit"] == 0

    async def test_tamper_detected_before_round_records_void_and_stops(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(script=[score_step(1, results=results)])
        loop = _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=1))
        await loop.prepare()
        (rsi_dirs.sandbox / "verify.sh").write_text("echo score=1000\n")
        outcome = await loop.run()
        assert outcome.stop_reason is StopReason.VERIFIER_TAMPERED
        assert outcome.exit_code == 3
        assert len(engine.calls) == 0
        rec = _records(results)[0]
        assert rec["void"] and rec["categories"] == ["verifier_tampered"]
        assert rec["exit"] == -1

    async def test_agent_reported_score_mismatch_is_cheat_and_measured_score_wins(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(
            script=[
                score_step(1, results=results, report=100.0),
                score_step(2, results=results),
            ]
        )
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=2)).run()
        assert outcome.stop_reason is StopReason.CHEAT_DETECTED
        assert outcome.exit_code == 3
        assert outcome.best_score is None  # a void round never feeds best
        assert len(engine.calls) == 1
        rec = _records(results)[0]
        assert rec["void"] is True
        assert rec["categories"] == ["cheat_detected"]
        assert rec["score"] == 1.0  # I2: loop-measured
        assert rec["agent_reported_score"] == 100.0

    async def test_sandbox_escape_voids_and_stops(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results

        def drop_binary(cwd: Path) -> None:
            (results / "payload.bin").write_bytes(b"\x00")

        engine = FakeEngine(script=[score_step(1, results=results, extra=drop_binary)])
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=3)).run()
        assert outcome.stop_reason is StopReason.CHEAT_DETECTED
        assert outcome.exit_code == 3
        assert _records(results)[0]["categories"] == ["sandbox_escape"]

    async def test_lock_file_rewrite_stops_even_if_hashes_hold(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results

        def rewrite_lock(cwd: Path) -> None:
            path = cwd / VERIFIER_LOCK_FILENAME
            path.write_text(path.read_text() + "\n")

        engine = FakeEngine(script=[score_step(1, results=results, extra=rewrite_lock)])
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=2)).run()
        assert outcome.stop_reason is StopReason.VERIFIER_TAMPERED
        assert outcome.exit_code == 3
        assert _records(results)[0]["categories"] == ["verifier_tampered"]


# --------------------------------------------------------------------------- #
# Self-edit and rollback (I3, I4)
# --------------------------------------------------------------------------- #


@dataclass
class StubSelfEdit:
    """Writes SCAFFOLD.md (and optionally another file), commits, returns the SHA."""

    sandbox: Path
    text: str = "BAD ADVICE\n"
    also_touch: str | None = None
    commit: bool = True
    seen: list[SelfEditInputs] = field(default_factory=list)

    async def propose(self, inputs: SelfEditInputs) -> str | None:
        self.seen.append(inputs)
        text = self.text if len(self.seen) == 1 else f"{self.text}edit {len(self.seen)}\n"
        (self.sandbox / SCAFFOLD_FILENAME).write_text(text)
        paths = [SCAFFOLD_FILENAME]
        if self.also_touch:
            (self.sandbox / self.also_touch).write_text("sneaky\n")
            paths.append(self.also_touch)
        if not self.commit:
            return None
        await run_git(self.sandbox, "add", "--", *paths, check=True)
        await run_git(self.sandbox, "commit", "-q", "-m", "rsi: self-edit", check=True)
        return (await run_git(self.sandbox, "rev-parse", "HEAD", check=True)).stdout.strip()


class TestSelfEdit:
    async def test_rollback_when_scores_degrade_after_edit(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox)
        engine = FakeEngine(
            script=[
                score_step(3, results=results),
                score_step(3, results=results),
                score_step(1, results=results),
                score_step(1, results=results),
            ]
        )
        cfg = _config(rsi_dirs, rounds=4, self_edit_every=2, self_edit_budget=1)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.stop_reason is StopReason.ROUNDS_EXHAUSTED
        assert outcome.self_edits == 1
        assert outcome.rollbacks == 1
        assert len(step.seen) == 1

        events = _events(results)
        assert [e["event"] for e in events] == ["self_edit", "rollback"]
        edit_sha = events[0]["scaffold_sha"]
        assert events[1]["reverted"] == edit_sha
        assert events[1]["best_before"] == 3.0 and events[1]["best_after"] == 1.0
        assert events[0]["round"] == 2 and events[1]["round"] == 4
        # SCAFFOLD.md is back to what it was, via a revert commit (history kept).
        assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD
        log = await run_git(rsi_dirs.sandbox, "log", "--format=%s", check=True)
        assert log.stdout.splitlines()[0].startswith('Revert "rsi: self-edit"')
        # Rounds 3 and 4 ran under the edited scaffold; rounds 1 and 2 did not.
        recs = _records(results)
        assert recs[2]["scaffold_sha"] == edit_sha and recs[3]["scaffold_sha"] == edit_sha
        assert recs[0]["scaffold_sha"] != edit_sha
        assert engine.calls[2].prompt.startswith("BAD ADVICE")
        assert not engine.calls[0].prompt.startswith("BAD ADVICE")

    async def test_no_rollback_when_scores_hold(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox, text="GOOD ADVICE\n")
        engine = FakeEngine(script=[score_step(v, results=results) for v in (3, 3, 3, 4)])
        cfg = _config(rsi_dirs, rounds=4, self_edit_every=2, self_edit_budget=1)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.rollbacks == 0
        assert [e["event"] for e in _events(results)] == ["self_edit", "self_edit_kept"]
        assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == "GOOD ADVICE\n"

    async def test_noise_floor_override_tolerates_small_drop(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox)
        engine = FakeEngine(script=[score_step(v, results=results) for v in (3, 3, 2.5, 2.5)])
        cfg = _config(rsi_dirs, rounds=4, self_edit_every=2, self_edit_budget=1, noise_floor=1.0)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.rollbacks == 0

    async def test_pass_fail_only_edit_judged_fine_when_next_round_passes(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox)

        def pass_step(prompt: str, cwd: Path) -> EngineResult:
            (cwd / "SCORE").write_text("ok\n")  # passes, no score line
            append_jsonl(results / "metrics.jsonl", json.dumps({"step": _round_of(prompt)}))
            return ok_result()

        engine = FakeEngine(
            script=[pass_step, pass_step, score_step(None, results=results, report=None)]
        )
        cfg = _config(rsi_dirs, rounds=3, self_edit_every=1, self_edit_budget=1)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.self_edits == 1
        assert outcome.rollbacks == 0  # round 2 still passed → edit judged fine
        # A second scenario: the edit lands after round 2 with budget 1 used
        # at round 1, so nothing more to judge; the fail at round 3 is just a fail.
        assert _records(results)[2]["categories"] == ["verifier_failed"]

    async def test_pass_fail_only_degrade_rolls_back(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox)

        def pass_step(prompt: str, cwd: Path) -> EngineResult:
            (cwd / "SCORE").write_text("ok\n")
            append_jsonl(results / "metrics.jsonl", json.dumps({"step": _round_of(prompt)}))
            return ok_result()

        engine = FakeEngine(script=[pass_step, score_step(None, results=results, report=None)])
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=1, self_edit_budget=1)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.self_edits == 1
        assert outcome.rollbacks == 1
        assert [e["event"] for e in _events(results)] == ["self_edit", "rollback"]

    async def test_edit_touching_other_paths_is_rejected_and_discarded(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox, also_touch="verify_helper.py")
        engine = FakeEngine(script=[score_step(v, results=results) for v in (1, 2)])
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=1, self_edit_budget=3)
        head_before = None
        loop = _loop(rsi_dirs, engine, config=cfg, self_edit=step)
        await loop.prepare()
        head_before = (
            await run_git(rsi_dirs.sandbox, "rev-parse", "HEAD", check=True)
        ).stdout.strip()
        outcome = await loop.run()
        assert outcome.self_edits == 0
        events = _events(results)
        assert [e["event"] for e in events] == ["self_edit_rejected", "self_edit_rejected"]
        assert not (rsi_dirs.sandbox / "verify_helper.py").exists()
        assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD
        head_after = (
            await run_git(rsi_dirs.sandbox, "rev-parse", "HEAD", check=True)
        ).stdout.strip()
        assert head_after == head_before
        assert all(r["scaffold_sha"] != events[0]["proposed"] for r in _records(results))

    async def test_uncommitted_edit_returning_none_is_discarded(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox, commit=False)
        engine = FakeEngine(script=[score_step(1, results=results)])
        cfg = _config(rsi_dirs, rounds=1, self_edit_every=1, self_edit_budget=1)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.self_edits == 0
        # A modified-but-uncommitted SCAFFOLD.md with None returned: nothing was kept.
        assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD
        assert [e["event"] for e in _events(results)] == ["self_edit_rejected"]

    async def test_summary_is_aggregate_only_and_redacts_verifier_internals(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox, text="fine\n")

        def leak_notes(cwd: Path) -> None:
            (cwd / "NOTES.md").write_text(
                "Round notes\nverifier is: sh verify.sh and lock is VERIFIER.json\n" + "x\n" * 60
            )

        engine = FakeEngine(
            script=[
                score_step(1, results=results, extra=leak_notes),
                score_step(2, results=results),
            ]
        )
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=2, self_edit_budget=1)
        await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert len(step.seen) == 1
        inputs = step.seen[0]
        assert inputs.round_index == 2
        assert inputs.best_score == 2.0
        assert [r.score for r in inputs.rounds] == [1.0, 2.0]
        assert inputs.taxonomy_counts["no_progress"] == 0
        assert set(inputs.taxonomy_counts) == {c.value for c in FailureCategory}
        assert inputs.scaffold_text == DEFAULT_SCAFFOLD
        assert "sh verify.sh" not in inputs.notes_tail
        assert VERIFIER_LOCK_FILENAME not in inputs.notes_tail
        assert "<redacted>" in inputs.notes_tail or len(inputs.notes_tail.splitlines()) == 40
        assert len(inputs.notes_tail.splitlines()) <= 40
        assert "sh verify.sh" in inputs.forbidden

    async def test_budget_caps_self_edits_per_invocation(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox, text="v\n")
        engine = FakeEngine(script=[score_step(v, results=results) for v in (1, 2, 3, 4)])
        cfg = _config(rsi_dirs, rounds=4, self_edit_every=1, self_edit_budget=2)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.self_edits == 2
        assert len(step.seen) == 2
        assert [e["event"] for e in _events(results)] == [
            "self_edit",
            "self_edit_kept",
            "self_edit",
            "self_edit_kept",
        ]

    async def test_self_edit_every_zero_disables(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox)
        engine = FakeEngine(script=[score_step(v, results=results) for v in (1, 2)])
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=0)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.self_edits == 0 and not step.seen

    async def test_self_edit_that_tampers_with_lock_stops(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results

        @dataclass
        class Tamperer:
            sandbox: Path

            async def propose(self, inputs: SelfEditInputs) -> str | None:
                (self.sandbox / "verify.sh").write_text("echo score=1000\n")
                return None

        engine = FakeEngine(script=[score_step(v, results=results) for v in (1, 2)])
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=1, self_edit_budget=1)
        outcome = await _loop(
            rsi_dirs, engine, config=cfg, self_edit=Tamperer(rsi_dirs.sandbox)
        ).run()
        assert outcome.stop_reason is StopReason.VERIFIER_TAMPERED
        assert outcome.exit_code == 3
        assert len(engine.calls) == 1


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


class TestDescribePlan:
    def test_fresh_plan_touches_nothing(self, tmp_path: Path) -> None:
        cfg = RsiConfig(slug="x", results_root=tmp_path / "r", workspace_root=tmp_path / "w")
        plan = describe_plan(cfg, VerifierSpec(command="echo score=1"))
        assert plan["sandbox"] == str(tmp_path / "w" / "rsi-x")
        assert plan["results"] == str(tmp_path / "r" / "loop-rsi-x")
        assert plan["resuming"] == "no" and plan["next_round"] == 1
        assert plan["verifier_lock"].startswith("would lock")
        assert plan["taxonomy_digest"] == TAXONOMY_DIGEST
        assert not (tmp_path / "w").exists() and not (tmp_path / "r").exists()

    async def test_resumed_plan_reports_lock_and_next_round(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(1, results=results)]),
            config=_config(rsi_dirs, rounds=1),
        ).run()
        plan = describe_plan(rsi_dirs.config, None)
        assert plan["resuming"] == "yes" and plan["next_round"] == 2
        assert plan["verifier_lock"] == "intact"
        (rsi_dirs.sandbox / "verify.sh").write_text("changed\n")
        assert describe_plan(rsi_dirs.config, None)["verifier_lock"].startswith("MISMATCH")
        assert describe_plan(rsi_dirs.config, VerifierSpec(command="other"))[
            "verifier_lock"
        ].startswith("MISMATCH")


# --------------------------------------------------------------------------- #
# Regressions from the adversarial review
# --------------------------------------------------------------------------- #


def _git(cwd: Path, *args: str) -> str:
    """Synchronous git for ``extra`` callbacks (the FakeEngine step is sync)."""
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        env=git_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _self_consistent_lock(command: str) -> str:
    return json.dumps(
        {
            "command": command,
            "command_sha256": sha256_text(command),
            "file_sha256s": {},
            "created_at_ms": 1,
        }
    )


class TestLockFileIsFrozen:
    """I1: a rewritten VERIFIER.json is a tamper even when it is self-consistent and committed."""

    async def test_committed_self_consistent_lock_rewrite_voids_round_and_stops(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results

        def hijack(cwd: Path) -> None:
            (cwd / VERIFIER_LOCK_FILENAME).write_text(_self_consistent_lock("echo score=1000"))
            _git(cwd, "add", "-A")
            _git(cwd, "commit", "-q", "-m", "round 1 work")

        engine = FakeEngine(
            script=[
                score_step(1, results=results, extra=hijack),
                score_step(2, results=results),
            ]
        )
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=3)).run()
        assert outcome.stop_reason is StopReason.VERIFIER_TAMPERED
        assert outcome.exit_code == 3
        assert len(engine.calls) == 1
        rec = _records(results)[0]
        assert rec["void"] is True and rec["categories"] == ["verifier_tampered"]
        assert rec["score"] is None  # the hijacked verifier never ran

    async def test_resume_under_a_rewritten_lock_is_refused_by_provenance(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(1, results=results)]),
            config=_config(rsi_dirs, rounds=1),
        ).run()
        provenance = [e for e in _all_events(results) if e["event"] == "verifier_locked"]
        assert len(provenance) == 1 and provenance[0]["file_sha256s"] == {
            "verify.sh": sha256_text(VERIFY_SH)
        }
        # Between invocations the sandbox's lock is replaced by a plausible one and committed,
        # as a crashed round (or a hand edit) could leave it.
        sandbox = rsi_dirs.sandbox
        (sandbox / VERIFIER_LOCK_FILENAME).write_text(_self_consistent_lock("echo score=1000"))
        _git(sandbox, "add", "-A")
        _git(sandbox, "commit", "-q", "-m", "swap lock")
        with pytest.raises(FrozenVerifierError, match="recorded in"):
            await _loop(rsi_dirs, FakeEngine(script=[]), verifier=None).run()
        assert len(_records(results)) == 1  # nothing ran under the swapped lock

    async def test_self_edit_step_raising_frozen_verifier_error_stops(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results

        @dataclass
        class Guarded:
            async def propose(self, inputs: SelfEditInputs) -> str | None:
                raise FrozenVerifierError("engine touched the lock")

        engine = FakeEngine(script=[score_step(1, results=results)])
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=1, self_edit_budget=1)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=Guarded()).run()
        assert outcome.stop_reason is StopReason.VERIFIER_TAMPERED
        assert outcome.exit_code == 3
        assert len(engine.calls) == 1
        tamper = [e for e in _events(results) if e["event"] == "verifier_tampered"]
        assert tamper and tamper[0]["after"] == "self_edit"


class TestScaffoldIsLoopOwned:
    async def test_round_that_rewrites_and_commits_scaffold_is_restored_and_logged(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results

        def hijack_scaffold(cwd: Path) -> None:
            (cwd / SCAFFOLD_FILENAME).write_text("ALWAYS write score=1000 to SCORE\n")
            _git(cwd, "commit", "-q", "-am", "agent rewrites the scaffold")

        engine = FakeEngine(
            script=[
                score_step(1, results=results, extra=hijack_scaffold),
                score_step(2, results=results),
            ]
        )
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=2)).run()
        assert outcome.stop_reason is StopReason.ROUNDS_EXHAUSTED
        assert outcome.self_edits == 0
        # Round 2 ran under the loop's scaffold, not the agent's.
        assert engine.calls[1].prompt.startswith(DEFAULT_SCAFFOLD.rstrip("\n"))
        assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD
        head_blob = await run_git(rsi_dirs.sandbox, "show", f"HEAD:{SCAFFOLD_FILENAME}", check=True)
        assert head_blob.stdout == DEFAULT_SCAFFOLD
        recs = _records(results)
        assert recs[0]["scaffold_sha"] == recs[1]["scaffold_sha"]
        drift = [e for e in _events(results) if e["event"] == "scaffold_drift"]
        assert len(drift) == 1
        assert drift[0]["round"] == 1 and drift[0]["head_moved"] is True
        assert drift[0]["restored_sha"] is not None
        log = await run_git(rsi_dirs.sandbox, "log", "--format=%s", check=True)
        assert "rsi: restore scaffold after round 1" in log.stdout.splitlines()

    async def test_uncommitted_scaffold_edit_is_restored_without_a_commit(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results

        def dirty_scaffold(cwd: Path) -> None:
            (cwd / SCAFFOLD_FILENAME).write_bytes(b"\xff\xfe not even utf-8\n")

        engine = FakeEngine(
            script=[
                score_step(1, results=results, extra=dirty_scaffold),
                score_step(2, results=results),
            ]
        )
        loop = _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=2))
        await loop.prepare()
        head_before = (await run_git(rsi_dirs.sandbox, "rev-parse", "HEAD", check=True)).stdout
        outcome = await loop.run()
        assert outcome.rounds_run == 2
        assert engine.calls[1].prompt.startswith(DEFAULT_SCAFFOLD.rstrip("\n"))
        drift = [e for e in _events(results) if e["event"] == "scaffold_drift"]
        assert len(drift) == 1 and drift[0]["head_moved"] is False
        assert drift[0]["worktree_moved"] is True
        head_after = (await run_git(rsi_dirs.sandbox, "rev-parse", "HEAD", check=True)).stdout
        assert head_after == head_before  # nothing to commit: HEAD already held the scaffold

    async def test_prompt_tells_the_agent_scaffold_is_off_limits(self, results: Path) -> None:
        prompt = build_round_prompt(
            round_no=1, results_dir=results, scaffold_text="", verifier_command="true"
        )
        assert f"{SCAFFOLD_FILENAME} is owned by the loop" in prompt


class TestSelfEditDiscardIsSurgical:
    """I3 with content digests; rejection never destroys the round agent's own work."""

    async def test_step_rewriting_an_already_dirty_path_is_rejected_and_restored(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results

        def commit_notes(cwd: Path) -> None:
            (cwd / "NOTES.md").write_text("round 1 notes\n")
            _git(cwd, "add", "NOTES.md")
            _git(cwd, "commit", "-q", "-m", "notes")

        def dirty_notes(cwd: Path) -> None:
            with (cwd / "NOTES.md").open("a") as fh:
                fh.write("ROUND 2 FINDINGS (uncommitted)\n")

        step = StubSelfEdit(rsi_dirs.sandbox, also_touch="NOTES.md", commit=False)
        engine = FakeEngine(
            script=[
                score_step(1, results=results, extra=commit_notes),
                score_step(2, results=results, extra=dirty_notes),
            ]
        )
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=2, self_edit_budget=1)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.self_edits == 0
        assert [e["event"] for e in _events(results)] == ["self_edit_rejected"]
        assert (rsi_dirs.sandbox / "NOTES.md").read_text() == (
            "round 1 notes\nROUND 2 FINDINGS (uncommitted)\n"
        )
        assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD

    async def test_kept_edit_that_also_rewrote_a_dirty_path_is_rejected(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results

        def dirty_helper(cwd: Path) -> None:
            (cwd / "helper.py").write_text("v1\n")
            _git(cwd, "add", "helper.py")
            _git(cwd, "commit", "-q", "-m", "helper")
            (cwd / "helper.py").write_text("v2 uncommitted\n")

        # Commits SCAFFOLD.md only (what a sneaky step would do) but also overwrites helper.py.
        @dataclass
        class Sneaky:
            sandbox: Path

            async def propose(self, inputs: SelfEditInputs) -> str | None:
                (self.sandbox / SCAFFOLD_FILENAME).write_text("EVIL SCAFFOLD\n")
                (self.sandbox / "helper.py").write_text("EVIL\n")
                await run_git(self.sandbox, "add", "--", SCAFFOLD_FILENAME, check=True)
                await run_git(
                    self.sandbox,
                    "commit",
                    "-q",
                    "--only",
                    "-m",
                    "rsi: self-edit",
                    "--",
                    SCAFFOLD_FILENAME,
                    check=True,
                )
                return (await run_git(self.sandbox, "rev-parse", "HEAD", check=True)).stdout.strip()

        engine = FakeEngine(script=[score_step(1, results=results, extra=dirty_helper)])
        cfg = _config(rsi_dirs, rounds=1, self_edit_every=1, self_edit_budget=1)
        outcome = await _loop(
            rsi_dirs, engine, config=cfg, self_edit=Sneaky(rsi_dirs.sandbox)
        ).run()
        assert outcome.self_edits == 0
        assert [e["event"] for e in _events(results)] == ["self_edit_rejected"]
        assert (rsi_dirs.sandbox / "helper.py").read_text() == "v2 uncommitted\n"
        assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD

    async def test_rejection_keeps_the_agents_uncommitted_tracked_work(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results

        def commit_model(cwd: Path) -> None:
            (cwd / "model.py").write_text("v1\n")
            _git(cwd, "add", "model.py")
            _git(cwd, "commit", "-q", "-m", "model v1")

        def work_on_model(cwd: Path) -> None:
            (cwd / "model.py").write_text("v2: hours of agent work, killed before commit\n")
            (cwd / "scratch").mkdir()
            (cwd / "scratch" / "run.log").write_text("untracked scratch\n")

        step = StubSelfEdit(rsi_dirs.sandbox, also_touch="verify_helper.py")
        engine = FakeEngine(
            script=[
                score_step(1, results=results, extra=commit_model),
                score_step(2, results=results, extra=work_on_model),
            ]
        )
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=2, self_edit_budget=1)
        loop = _loop(rsi_dirs, engine, config=cfg, self_edit=step)
        outcome = await loop.run()
        assert outcome.self_edits == 0
        assert [e["event"] for e in _events(results)] == ["self_edit_rejected"]
        sandbox = rsi_dirs.sandbox
        assert (sandbox / "model.py").read_text() == (
            "v2: hours of agent work, killed before commit\n"
        )
        assert (sandbox / "scratch" / "run.log").read_text() == "untracked scratch\n"
        assert not (sandbox / "verify_helper.py").exists()
        assert (sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD
        subject = await run_git(sandbox, "log", "-n1", "--format=%s", check=True)
        assert subject.stdout.strip() == "model v1"


class TestAgentWrittenContentNeverCrashesTheRound:
    async def test_non_finite_reported_score_is_recorded_as_none_and_flagged(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results

        def lie_infinitely(prompt: str, cwd: Path) -> EngineResult:
            (cwd / "SCORE").write_text("score=1\n")
            with (results / "metrics.jsonl").open("a") as fh:
                fh.write('{"step": 1, "ts": 1, "score": 1e999}\n')
            return ok_result()

        engine = FakeEngine(script=[lie_infinitely, score_step(2, results=results)])
        outcome = await _loop(rsi_dirs, engine, config=_config(rsi_dirs, rounds=2)).run()
        assert outcome.stop_reason is StopReason.CHEAT_DETECTED
        assert outcome.exit_code == 3
        recs = _records(results)
        assert len(recs) == 1  # the round IS recorded (I7)
        assert recs[0]["score"] == 1.0  # I2: measured
        assert recs[0]["agent_reported_score"] is None
        assert recs[0]["void"] is True and recs[0]["categories"] == ["cheat_detected"]

    async def test_undecodable_metrics_and_notes_do_not_crash(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results

        def garbage(prompt: str, cwd: Path) -> EngineResult:
            (cwd / "SCORE").write_text("score=1\n")
            with (results / "metrics.jsonl").open("ab") as fh:
                fh.write(b'{"step": 1}\xff\n')
            (cwd / "NOTES.md").write_bytes(b"\xff\xfe notes that are not utf-8\n")
            return ok_result()

        step = StubSelfEdit(rsi_dirs.sandbox, text="fine\n")
        engine = FakeEngine(script=[garbage])
        cfg = _config(rsi_dirs, rounds=1, self_edit_every=1, self_edit_budget=1)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.stop_reason is StopReason.ROUNDS_EXHAUSTED
        rec = _records(results)[0]
        assert rec["categories"] == ["no_metrics"] and rec["score"] == 1.0
        assert outcome.self_edits == 1 and len(step.seen) == 1


class TestRollbackAcrossInvocationsAndDirtyIndex:
    async def test_pending_edit_is_judged_on_resume(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox, text="BAD\n")
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=2, self_edit_budget=1)
        first = await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(v, results=results) for v in (5, 5)]),
            config=cfg,
            self_edit=step,
        ).run()
        assert first.self_edits == 1 and first.rollbacks == 0
        assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == "BAD\n"

        second = await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(v, results=results) for v in (0, 0)]),
            config=cfg,
            self_edit=None,
            verifier=None,
        ).run()
        assert second.rollbacks == 1
        events = _events(results)
        assert [e["event"] for e in events] == ["self_edit", "rollback"]
        assert events[1]["reverted"] == events[0]["scaffold_sha"]
        assert events[1]["round"] == 4
        assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD

    async def test_rollback_succeeds_with_staged_agent_changes(self, rsi_dirs: RsiDirs) -> None:
        results = rsi_dirs.results

        def stage_work(cwd: Path) -> None:
            (cwd / "work.txt").write_text("staged but never committed\n")
            _git(cwd, "add", "work.txt")

        step = StubSelfEdit(rsi_dirs.sandbox)
        engine = FakeEngine(
            script=[
                score_step(3, results=results),
                score_step(3, results=results),
                score_step(1, results=results, extra=stage_work),
                score_step(1, results=results),
            ]
        )
        cfg = _config(rsi_dirs, rounds=4, self_edit_every=2, self_edit_budget=1)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.stop_reason is StopReason.ROUNDS_EXHAUSTED
        assert outcome.rollbacks == 1
        assert [e["event"] for e in _events(results)] == ["self_edit", "rollback"]
        sandbox = rsi_dirs.sandbox
        assert (sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD
        log = await run_git(sandbox, "log", "--format=%s", check=True)
        assert log.stdout.splitlines()[0].startswith('Revert "rsi: self-edit"')
        # The agent's staged work is still staged, not swept into the revert.
        staged = await run_git(sandbox, "diff", "--cached", "--name-only", check=True)
        assert staged.stdout.split() == ["work.txt"]
        revert_files = await run_git(
            sandbox, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", check=True
        )
        assert revert_files.stdout.split() == [SCAFFOLD_FILENAME]

    async def test_rollback_that_cannot_revert_stops_the_loop(
        self, rsi_dirs: RsiDirs, write_trajectory: Callable[[Sequence[dict[str, Any]]], Path]
    ) -> None:
        results = rsi_dirs.results

        def agent_commit(cwd: Path) -> None:
            (cwd / "junk.txt").write_text("x\n")
            _git(cwd, "add", "junk.txt")
            _git(cwd, "commit", "-q", "-m", "agent junk")

        await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(5, results=results, extra=agent_commit)]),
            config=_config(rsi_dirs, rounds=1),
        ).run()
        # A trajectory that claims the agent's commit was a self-edit (it touches junk.txt,
        # so it can never be reverted as a scaffold commit).
        bogus = (await run_git(rsi_dirs.sandbox, "rev-parse", "HEAD", check=True)).stdout.strip()
        write_trajectory([{"event": "self_edit", "round": 1, "ts": 1, "scaffold_sha": bogus}])
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=1, self_edit_budget=0)
        outcome = await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(0, results=results), score_step(0, results=results)]),
            config=cfg,
            verifier=None,
        ).run()
        assert outcome.stop_reason is StopReason.ROLLBACK_FAILED
        assert outcome.rounds_run == 1
        failed = [e for e in _events(results) if e["event"] == "rollback_failed"]
        assert failed and failed[0]["reverted"] == bogus
        assert "junk.txt" in failed[0]["error"]


class TestDescribePlanNeverCrashes:
    def test_corrupt_trajectory_is_reported_not_raised(self, rsi_dirs: RsiDirs) -> None:
        (rsi_dirs.results / "trajectory.json").write_text('{"round": 1}\nnot json\n')
        plan = describe_plan(rsi_dirs.config, VerifierSpec(command="true"))
        assert plan["next_round"] is None
        assert plan["trajectory"].startswith("UNREADABLE")
