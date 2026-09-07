"""Regression coverage for restart and cancellation authority boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from turing.research.rsi.contracts import EngineResult, RoundRecord
from turing.research.rsi.engine import FakeEngine, ok_result
from turing.research.rsi.loop import append_jsonl, read_trajectory

from .test_loop import StubSelfEdit, _config, _events, _loop, score_step

if TYPE_CHECKING:
    from pathlib import Path

    from .conftest import RsiDirs


def _failed_score_step(value: float, results: Path):
    successful = score_step(value, results=results)

    def step(prompt: str, cwd: Path) -> EngineResult:
        successful(prompt, cwd)
        return replace(ok_result(), exit_code=1)

    return step


async def test_pending_window_is_settled_before_terminal_engine_failure(
    rsi_dirs: RsiDirs,
) -> None:
    results = rsi_dirs.results
    step = StubSelfEdit(rsi_dirs.sandbox, text="BAD\n")
    engine = FakeEngine(
        script=[
            score_step(5, results=results),
            score_step(5, results=results),
            score_step(5, results=results),
            _failed_score_step(1, results),
            _failed_score_step(1, results),
            _failed_score_step(1, results),
        ]
    )

    outcome = await _loop(
        rsi_dirs,
        engine,
        config=_config(rsi_dirs, rounds=6, self_edit_every=3, self_edit_budget=1),
        self_edit=step,
    ).run()

    assert outcome.stop_reason.value == "engine_failures"
    assert outcome.rollbacks == 1
    events = _events(results)
    assert [(event["event"], event["round"]) for event in events] == [
        ("self_edit", 3),
        ("rollback", 6),
    ]
    assert (rsi_dirs.sandbox / "SCAFFOLD.md").read_text() != "BAD\n"


async def test_completed_pending_history_is_settled_before_restart_engine_call(
    rsi_dirs: RsiDirs,
) -> None:
    results = rsi_dirs.results
    step = StubSelfEdit(rsi_dirs.sandbox, text="BAD\n")
    first = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(5, results=results)]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=1, self_edit_budget=1),
        self_edit=step,
    ).run()
    assert first.self_edits == 1
    edit = _events(results)[0]

    # Model a process stop after round 2's verifier result but before the
    # judgment event was appended. The commit and the row are trusted setup;
    # the resumed loop must settle them before constructing round 3's prompt.
    append_jsonl(
        results / "trajectory.json",
        RoundRecord(
            round=2,
            started=2,
            ended=3,
            exit=0,
            score=1.0,
            passed=True,
            scaffold_sha=edit["scaffold_sha"],
        ).to_json_line(),
    )

    resumed_engine = FakeEngine(script=[score_step(1, results=results)])
    resumed = await _loop(
        rsi_dirs,
        resumed_engine,
        config=_config(rsi_dirs, rounds=1, self_edit_every=0, self_edit_budget=0),
        self_edit=None,
        verifier=None,
    ).run()

    assert resumed.rollbacks == 1
    events = _events(results)
    assert [(event["event"], event["round"]) for event in events] == [
        ("self_edit", 1),
        ("rollback", 2),
    ]
    assert not resumed_engine.calls[0].prompt.startswith("BAD\n")


class _CancelledEdit:
    def __init__(self, results: Path) -> None:
        self.results = results
        self.ready = asyncio.Event()
        self.block = asyncio.Event()

    async def propose(self, inputs):
        del inputs
        with (self.results / "trajectory.json").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "round": 777,
                        "started": 1,
                        "ended": 2,
                        "exit": 0,
                        "score": 999.0,
                        "passed": True,
                        "categories": [],
                        "void": False,
                    }
                )
                + "\n"
            )
        self.ready.set()
        await self.block.wait()
        return None


async def test_cancellation_during_self_edit_restores_trajectory_before_resume(
    rsi_dirs: RsiDirs,
) -> None:
    edit = _CancelledEdit(rsi_dirs.results)
    cfg = _config(rsi_dirs, rounds=1, self_edit_every=1, self_edit_budget=1)
    task = asyncio.create_task(
        _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(1, results=rsi_dirs.results)]),
            config=cfg,
            self_edit=edit,
        ).run()
    )
    await edit.ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    trajectory = rsi_dirs.results / "trajectory.json"
    assert all(json.loads(line).get("round") != 777 for line in trajectory.read_text().splitlines())
    evidence = sorted(rsi_dirs.results.glob("trajectory.tamper-round-1*.json"))
    assert len(evidence) == 1

    resumed = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(1, results=rsi_dirs.results)]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=0, self_edit_budget=0),
        self_edit=None,
        verifier=None,
    ).run()
    assert resumed.best_score == 1.0
    assert all(record.round != 777 for record in read_trajectory(trajectory).records)
