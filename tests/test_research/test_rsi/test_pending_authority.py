"""Authority and restart coverage for pending RSI self-edit judgments."""

from __future__ import annotations

import asyncio
import json

import pytest

from turing.research.contracts import ContractViolationError
from turing.research.rsi import cli
from turing.research.rsi.cli import ALLOW_FAKE_ENGINE_ENV, EXIT_ENGINE_FAILURE
from turing.research.rsi.engine import FakeEngine
from turing.research.rsi.loop import DEFAULT_SCAFFOLD, SCAFFOLD_FILENAME, StopReason

from .test_loop import StubSelfEdit, _config, _events, _git, _loop, run_git, score_step


async def test_pending_window_survives_schedule_changes_and_disabled_proposals(rsi_dirs) -> None:
    results = rsi_dirs.results
    step = StubSelfEdit(rsi_dirs.sandbox, text="BAD\n")

    first = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(v, results=results) for v in (5, 5)]),
        config=_config(rsi_dirs, rounds=2, self_edit_every=2, self_edit_budget=1),
        self_edit=step,
    ).run()
    assert first.self_edits == 1
    edit = _events(results)[0]
    assert edit["event"] == "self_edit"
    assert edit["judgment_window"] == 2

    second = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(1, results=results)]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=1, self_edit_budget=0),
        self_edit=None,
        verifier=None,
    ).run()
    assert second.rollbacks == 0
    assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == "BAD\n"

    # The persisted two-round window closes here even though this invocation
    # disables all new proposals with self_edit_every=0.
    third = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(1, results=results)]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=0, self_edit_budget=0),
        self_edit=None,
        verifier=None,
    ).run()
    assert third.rollbacks == 1
    rollback = _events(results)[-1]
    assert rollback["event"] == "rollback"
    assert rollback["judgment_window"] == 2
    assert rollback["judgment_window_source"] == "recorded"
    assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD
    assert len(step.seen) == 1


async def test_legacy_pending_event_uses_explicit_disabled_fallback(rsi_dirs) -> None:
    results = rsi_dirs.results
    step = StubSelfEdit(rsi_dirs.sandbox, text="LEGACY\n")
    first = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(v, results=results) for v in (5, 5)]),
        config=_config(rsi_dirs, rounds=2, self_edit_every=2, self_edit_budget=1),
        self_edit=step,
    ).run()
    assert first.self_edits == 1

    trajectory = results / "trajectory.json"
    lines = [json.loads(line) for line in trajectory.read_text().splitlines() if line.strip()]
    for line in lines:
        if line.get("event") == "self_edit":
            line.pop("judgment_window", None)
    trajectory.write_text("".join(json.dumps(line) + "\n" for line in lines))

    resumed = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(5, results=results)]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=0, self_edit_budget=0),
        self_edit=None,
        verifier=None,
    ).run()
    assert resumed.rollbacks == 0
    kept = _events(results)[-1]
    assert kept["event"] == "self_edit_kept"
    assert kept["judgment_window"] == 1
    assert kept["judgment_window_source"] == "legacy_minimum"


async def test_pending_edit_missing_commit_refuses_resume(rsi_dirs) -> None:
    results = rsi_dirs.results
    step = StubSelfEdit(rsi_dirs.sandbox, text="BAD\n")
    first = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(5, results=results)]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=1, self_edit_budget=1),
        self_edit=step,
    ).run()
    assert first.self_edits == 1
    await run_git(rsi_dirs.sandbox, "reset", "--hard", "HEAD^", check=True)

    with pytest.raises(ContractViolationError, match="no longer in HEAD's history"):
        await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(1, results=results)]),
            config=_config(rsi_dirs, rounds=1, self_edit_every=0, self_edit_budget=0),
            self_edit=None,
            verifier=None,
        ).run()


async def test_pending_edit_blob_mismatch_refuses_resume(rsi_dirs) -> None:
    results = rsi_dirs.results
    step = StubSelfEdit(rsi_dirs.sandbox, text="BAD\n")
    first = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(5, results=results)]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=1, self_edit_budget=1),
        self_edit=step,
    ).run()
    assert first.self_edits == 1

    trajectory = results / "trajectory.json"
    lines = [json.loads(line) for line in trajectory.read_text().splitlines() if line.strip()]
    for line in lines:
        if line.get("event") == "self_edit":
            line["scaffold_blob"] = "0" * 40
    trajectory.write_text("".join(json.dumps(line) + "\n" for line in lines))

    with pytest.raises(ContractViolationError) as exc_info:
        await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(1, results=results)]),
            config=_config(rsi_dirs, rounds=1, self_edit_every=0, self_edit_budget=0),
            self_edit=None,
            verifier=None,
        ).run()
    assert "scaffold" in str(exc_info.value)


async def test_persisted_rollback_failure_refuses_resume(rsi_dirs) -> None:
    results = rsi_dirs.results

    def agent_commit(cwd):
        (cwd / "junk.txt").write_text("agent commit\n")
        _git(cwd, "add", "junk.txt")
        _git(cwd, "commit", "-q", "-m", "agent junk")

    first = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(5, results=results, extra=agent_commit)]),
        config=_config(rsi_dirs, rounds=1),
    ).run()
    assert first.rounds_run == 1
    bogus = (await run_git(rsi_dirs.sandbox, "rev-parse", "HEAD", check=True)).stdout.strip()
    with (results / "trajectory.json").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"event": "self_edit", "round": 1, "ts": 1, "scaffold_sha": bogus}) + "\n"
        )

    failed = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(0, results=results)]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=1, self_edit_budget=0),
        verifier=None,
    ).run()
    assert failed.stop_reason is StopReason.ROLLBACK_FAILED
    assert failed.exit_code == EXIT_ENGINE_FAILURE
    assert any(event["event"] == "rollback_failed" for event in _events(results))

    with pytest.raises(ContractViolationError, match="persisted rollback_failed"):
        await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(0, results=results)]),
            config=_config(rsi_dirs, rounds=1, self_edit_every=0, self_edit_budget=0),
            self_edit=None,
            verifier=None,
        ).run()


def test_exported_cli_returns_failure_on_rollback_failed(rsi_dirs, monkeypatch) -> None:
    results = rsi_dirs.results

    def agent_commit(cwd):
        (cwd / "junk.txt").write_text("agent commit\n")
        _git(cwd, "add", "junk.txt")
        _git(cwd, "commit", "-q", "-m", "agent junk")

    async def seed() -> None:
        first = await _loop(
            rsi_dirs,
            FakeEngine(script=[score_step(5, results=results, extra=agent_commit)]),
            config=_config(rsi_dirs, rounds=1, self_edit_every=0),
        ).run()
        assert first.rounds_run == 1

    asyncio.run(seed())
    bogus = _git(rsi_dirs.sandbox, "rev-parse", "HEAD")
    with (results / "trajectory.json").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"event": "self_edit", "round": 1, "ts": 1, "scaffold_sha": bogus}) + "\n"
        )

    monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
    monkeypatch.setattr(
        cli,
        "_build_engine",
        lambda _name, *, results_dir=None: FakeEngine(script=[score_step(0, results=results)]),
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
            "fake",
            "--rounds",
            "1",
            "--self-edit-every",
            "1",
            "--self-edit-budget",
            "0",
        ]
    )

    assert code == EXIT_ENGINE_FAILURE
