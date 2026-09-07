"""Invocation status stays distinct from the historical round facts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from turing.research.rsi import cli
from turing.research.rsi.cli import EXIT_ENGINE_FAILURE
from turing.research.rsi.contracts import EngineResult, VerifierSpec
from turing.research.rsi.engine import FakeEngine, ok_result
from turing.research.rsi.loop import StopReason

from .test_loop import _config, _loop, _records, score_step

if TYPE_CHECKING:
    from pathlib import Path

    from .conftest import RsiDirs


PASSING_VERIFIER = VerifierSpec(command='printf "score=1\\n"')


def _failed(*, exit_code: int, timed_out: bool = False) -> EngineResult:
    return EngineResult(
        exit_code=exit_code,
        stdout="",
        stderr="engine failed",
        wall_seconds=0.1,
        timed_out=timed_out,
    )


@pytest.mark.parametrize(
    ("exit_code", "timed_out"),
    [(127, False), (124, True), (125, False)],
)
async def test_all_failed_engine_attempts_fail_the_invocation_but_keep_verifier_facts(
    rsi_dirs: RsiDirs, exit_code: int, timed_out: bool
) -> None:
    outcome = await _loop(
        rsi_dirs,
        FakeEngine(script=[_failed(exit_code=exit_code, timed_out=timed_out)]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=0),
        verifier=PASSING_VERIFIER,
    ).run()

    assert outcome.exit_code == EXIT_ENGINE_FAILURE
    assert outcome.engine_failures == 1
    assert outcome.stop_reason is StopReason.ROUNDS_EXHAUSTED
    record = _records(rsi_dirs.results)[0]
    assert record["exit"] == exit_code
    assert record["passed"] is True
    assert record["score"] == 1.0
    expected_category = "timeout" if timed_out else "engine_error"
    assert expected_category in record["categories"]


async def test_recovered_engine_then_final_failure_is_not_all_failed(
    rsi_dirs: RsiDirs,
) -> None:
    def failed_round(_prompt: str, cwd: Path) -> EngineResult:
        (cwd / "SCORE").unlink(missing_ok=True)
        return _failed(exit_code=127)

    outcome = await _loop(
        rsi_dirs,
        FakeEngine(
            script=[
                failed_round,
                score_step(1, results=rsi_dirs.results),
                failed_round,
            ]
        ),
        config=_config(rsi_dirs, rounds=3, self_edit_every=0),
    ).run()

    assert outcome.exit_code == 0
    assert outcome.engine_failures == 2
    assert [record["exit"] for record in _records(rsi_dirs.results)] == [127, 0, 127]
    assert [record["score"] for record in _records(rsi_dirs.results)] == [None, 1.0, None]


async def test_successful_engine_with_failed_verifier_remains_a_normal_invocation(
    rsi_dirs: RsiDirs,
) -> None:
    outcome = await _loop(
        rsi_dirs,
        FakeEngine(script=[ok_result()]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=0),
    ).run()

    assert outcome.exit_code == 0
    assert outcome.engine_failures == 0
    record = _records(rsi_dirs.results)[0]
    assert record["passed"] is False
    assert set(record["categories"]) == {"no_metrics", "verifier_failed"}


async def test_stop_resume_with_zero_rounds_is_success_even_after_failed_history(
    rsi_dirs: RsiDirs,
) -> None:
    first = await _loop(
        rsi_dirs,
        FakeEngine(script=[_failed(exit_code=127)]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=0),
        verifier=PASSING_VERIFIER,
    ).run()
    assert first.exit_code == EXIT_ENGINE_FAILURE
    (rsi_dirs.sandbox / "STOP").touch()

    resumed = await _loop(
        rsi_dirs,
        FakeEngine(script=[]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=0),
        verifier=PASSING_VERIFIER,
    ).run()

    assert resumed.rounds_run == 0
    assert resumed.engine_failures == 0
    assert resumed.stop_reason is StopReason.STOP_FILE
    assert resumed.exit_code == 0


def test_exported_cli_returns_failure_for_an_all_failed_invocation(
    rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "_build_engine",
        lambda _name, *, results_dir=None: FakeEngine(script=[_failed(exit_code=127)]),
    )

    code = cli.main(
        [
            "--slug",
            rsi_dirs.config.slug,
            "--results-root",
            str(rsi_dirs.config.results_root),
            "--workspace-root",
            str(rsi_dirs.config.workspace_root),
            "--engine",
            "codex",
            "--verifier",
            'printf "score=1\\n"',
            "--problem",
            "goal",
            "--rounds",
            "1",
            "--self-edit-every",
            "0",
        ]
    )

    assert code == EXIT_ENGINE_FAILURE
    record = json.loads((rsi_dirs.results / "trajectory.json").read_text().splitlines()[-1])
    assert record["exit"] == 127
    assert record["passed"] is True
