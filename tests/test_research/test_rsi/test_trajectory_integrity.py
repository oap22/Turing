"""Regression coverage for supervisor-owned RSI trajectory evidence."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import ContractViolationError
from turing.research.rsi.cheat import CheatDetector, run_git
from turing.research.rsi.contracts import RsiConfig, VerifierSpec
from turing.research.rsi.engine import FakeEngine, ok_result
from turing.research.rsi.loop import DEFAULT_SCAFFOLD, RsiLoop, read_trajectory
from turing.research.rsi.scaffold import SCAFFOLD_FILENAME
from turing.research.rsi.taxonomy import FailureCategory

if TYPE_CHECKING:
    from pathlib import Path

    from .conftest import RsiDirs


def _forged_line() -> bytes:
    return (
        json.dumps(
            {
                "round": 777,
                "started": 1,
                "ended": 2,
                "exit": 0,
                "score": 999.0,
                "passed": True,
                "categories": [],
                "scaffold_sha": None,
                "void": False,
                "agent_reported_score": None,
                "verifier_wall_seconds": 0.0,
            }
        ).encode()
        + b"\n"
    )


def _trajectory_action(dirs: RsiDirs, mutation: str):
    def action(prompt: str, cwd: Path):
        del prompt, cwd
        trajectory = dirs.config.results_dir / "trajectory.json"
        forged = _forged_line()
        if mutation == "append":
            with trajectory.open("ab") as stream:
                stream.write(forged)
        elif mutation == "replace":
            trajectory.write_bytes(forged)
        elif mutation == "delete":
            trajectory.unlink()
        with (dirs.config.results_dir / "metrics.jsonl").open("a") as stream:
            stream.write('{"step":1,"score":1}\n')
        return ok_result()

    return action


class _SelfEdit:
    def __init__(
        self,
        sandbox: Path,
        results: Path,
        *,
        mutate_trajectory: bool,
        fail_after_mutation: bool = False,
    ) -> None:
        self.sandbox = sandbox
        self.results = results
        self.mutate_trajectory = mutate_trajectory
        self.fail_after_mutation = fail_after_mutation

    async def propose(self, inputs) -> str:
        del inputs
        if self.mutate_trajectory:
            trajectory = self.results / "trajectory.json"
            before = trajectory.stat()
            original = trajectory.read_bytes()
            changed = original.replace(b'"score": 1.0', b'"score": 9.0', 1)
            assert changed != original and len(changed) == len(original)
            trajectory.write_bytes(changed)
            os.utime(trajectory, ns=(before.st_atime_ns, before.st_mtime_ns))
            if self.fail_after_mutation:
                raise ContractViolationError("self-edit proposal failed after writing history")
        (self.sandbox / SCAFFOLD_FILENAME).write_text("SELF EDIT\n")
        await run_git(self.sandbox, "add", "--", SCAFFOLD_FILENAME, check=True)
        await run_git(self.sandbox, "commit", "-q", "-m", "rsi: self-edit", check=True)
        return (await run_git(self.sandbox, "rev-parse", "HEAD", check=True)).stdout.strip()


def _score_one(results: Path):
    def action(prompt: str, cwd: Path):
        del prompt, cwd
        with (results / "metrics.jsonl").open("a") as stream:
            stream.write('{"step":1,"score":1}\n')
        return ok_result()

    return action


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["append", "replace", "delete"])
async def test_trajectory_mutation_voids_round_and_preserves_evidence(
    rsi_dirs: RsiDirs, mutation: str
) -> None:
    engine = FakeEngine(script=[_trajectory_action(rsi_dirs, mutation)])
    loop = RsiLoop(
        rsi_dirs.config,
        engine=engine,
        verifier=VerifierSpec(command="sh verify.sh", files=("verify.sh",)),
        self_edit=None,
        cheat=CheatDetector(),
        problem="protect trajectory evidence",
    )
    (rsi_dirs.sandbox / "verify.sh").write_text("printf 'score=1\\n'\n")

    outcome = await loop.run()

    assert outcome.exit_code == 3
    state = read_trajectory(rsi_dirs.config.results_dir / "trajectory.json")
    assert state.best_score is None
    assert all(record.round != 777 for record in state.records)
    record = state.records[-1]
    assert record.void
    assert FailureCategory.CHEAT_DETECTED in record.categories
    evidence = sorted(rsi_dirs.config.results_dir.glob("trajectory.tamper-round-1*.json"))
    assert len(evidence) == 1
    if mutation == "delete":
        assert b'"kind": "missing"' in evidence[0].read_bytes()
    else:
        assert b"777" in evidence[0].read_bytes()


@pytest.mark.asyncio
async def test_metrics_append_remains_valid_and_restart_cannot_promote_forged_score(
    rsi_dirs: RsiDirs,
) -> None:
    results = rsi_dirs.config.results_dir
    (rsi_dirs.sandbox / "verify.sh").write_text("printf 'score=1\\n'\n")

    first = RsiLoop(
        rsi_dirs.config,
        engine=FakeEngine(script=[_trajectory_action(rsi_dirs, "append")]),
        verifier=VerifierSpec(command="sh verify.sh", files=("verify.sh",)),
        self_edit=None,
        cheat=CheatDetector(),
        problem="protect trajectory evidence",
    )
    assert (await first.run()).exit_code == 3

    def normal(prompt: str, cwd: Path):
        del prompt, cwd
        with (results / "metrics.jsonl").open("a") as stream:
            stream.write('{"step":2,"score":1}\n')
        return ok_result()

    second = RsiLoop(
        replace(rsi_dirs.config, rounds=1),
        engine=FakeEngine(script=[normal]),
        verifier=None,
        self_edit=None,
        cheat=CheatDetector(),
        # One clean post-tamper round is enough to prove resume bookkeeping.
        problem="protect trajectory evidence",
    )
    outcome = await second.run()

    state = read_trajectory(results / "trajectory.json")
    assert outcome.exit_code == 0
    assert outcome.best_score == 1.0
    assert state.best_score == 1.0
    assert all(record.round != 777 and record.score != 999.0 for record in state.records)


@pytest.mark.asyncio
async def test_late_verifier_write_is_quarantined_before_resume(
    rsi_dirs: RsiDirs,
) -> None:
    results = rsi_dirs.config.results_dir
    trajectory = results / "trajectory.json"
    marker = rsi_dirs.sandbox / "late-write-complete"
    forged = _forged_line().decode()
    (rsi_dirs.sandbox / "verify.sh").write_text(
        "printf 'score=1\\n'\n"
        f"if [ ! -e {shlex.quote(str(marker))} ]; then\n"
        "    sleep 0.05\n"
        f"    printf '%s' {shlex.quote(forged)} >> {shlex.quote(str(trajectory))}\n"
        f"    : > {shlex.quote(str(marker))}\n"
        "fi\n"
    )

    def clean_round(step: int):
        def action(prompt: str, cwd: Path):
            del prompt, cwd
            with (results / "metrics.jsonl").open("a") as stream:
                stream.write(json.dumps({"step": step, "score": 1}) + "\n")
            return ok_result()

        return action

    first = RsiLoop(
        rsi_dirs.config,
        engine=FakeEngine(script=[clean_round(1)]),
        verifier=VerifierSpec(command="sh verify.sh", files=("verify.sh",)),
        self_edit=None,
        cheat=CheatDetector(),
        problem="protect late verifier trajectory writes",
    )
    assert (await first.run()).exit_code == 3

    state_after_tamper = read_trajectory(trajectory)
    assert state_after_tamper.best_score is None
    assert all(record.round != 777 for record in state_after_tamper.records)
    evidence = sorted(results.glob("trajectory.tamper-round-1*.json"))
    assert len(evidence) == 1
    assert b"777" in evidence[0].read_bytes()

    second = RsiLoop(
        replace(rsi_dirs.config, rounds=1),
        engine=FakeEngine(script=[clean_round(2)]),
        verifier=None,
        self_edit=None,
        cheat=CheatDetector(),
        problem="protect late verifier trajectory writes",
    )
    outcome = await second.run()

    state_after_resume = read_trajectory(trajectory)
    assert outcome.exit_code == 0
    assert outcome.best_score == 1.0
    assert state_after_resume.best_score == 1.0
    assert all(
        record.round != 777 and record.score != 999.0 for record in state_after_resume.records
    )


@pytest.mark.asyncio
async def test_self_edit_same_stat_history_mutation_is_rejected_and_restored(
    tmp_path: Path,
) -> None:
    cfg = RsiConfig(
        slug="self-edit-history",
        workspace_root=tmp_path / "workspace",
        results_root=tmp_path / "results",
        rounds=1,
        self_edit_every=1,
        self_edit_budget=1,
    )
    cfg.sandbox_dir.mkdir(parents=True)
    (cfg.sandbox_dir / "verify.sh").write_text("printf 'score=1\\n'\n")
    edit = _SelfEdit(cfg.sandbox_dir, cfg.results_dir, mutate_trajectory=True)

    outcome = await RsiLoop(
        cfg,
        engine=FakeEngine(script=[_score_one(cfg.results_dir)]),
        verifier=VerifierSpec(command="sh verify.sh", files=("verify.sh",)),
        self_edit=edit,
        cheat=CheatDetector(),
        problem="reject self-edit history mutation",
    ).run()

    state = read_trajectory(cfg.results_dir / "trajectory.json")
    assert outcome.exit_code == 0
    assert outcome.self_edits == 0
    assert state.best_score == 1.0
    assert state.records[-1].score == 1.0
    assert state.events[-1].event == "self_edit_rejected"
    assert "trajectory.json" in state.events[-1].details["reason"]
    evidence = sorted(cfg.results_dir.glob("trajectory.tamper-round-1*.json"))
    assert len(evidence) == 1
    assert b'"score": 9.0' in evidence[0].read_bytes()
    assert (cfg.sandbox_dir / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD


@pytest.mark.asyncio
async def test_failed_self_edit_history_mutation_is_restored(tmp_path: Path) -> None:
    cfg = RsiConfig(
        slug="failed-self-edit-history",
        workspace_root=tmp_path / "workspace",
        results_root=tmp_path / "results",
        rounds=1,
        self_edit_every=1,
        self_edit_budget=1,
    )
    cfg.sandbox_dir.mkdir(parents=True)
    (cfg.sandbox_dir / "verify.sh").write_text("printf 'score=1\\n'\n")
    edit = _SelfEdit(
        cfg.sandbox_dir,
        cfg.results_dir,
        mutate_trajectory=True,
        fail_after_mutation=True,
    )

    with pytest.raises(ContractViolationError, match="after writing history"):
        await RsiLoop(
            cfg,
            engine=FakeEngine(script=[_score_one(cfg.results_dir)]),
            verifier=VerifierSpec(command="sh verify.sh", files=("verify.sh",)),
            self_edit=edit,
            cheat=CheatDetector(),
            problem="reject failed self-edit history mutation",
        ).run()

    state = read_trajectory(cfg.results_dir / "trajectory.json")
    assert state.best_score == 1.0
    assert state.records[-1].score == 1.0
    evidence = sorted(cfg.results_dir.glob("trajectory.tamper-round-1*.json"))
    assert len(evidence) == 1
    assert b'"score": 9.0' in evidence[0].read_bytes()


@pytest.mark.asyncio
async def test_ordinary_self_edit_remains_valid(tmp_path: Path) -> None:
    cfg = RsiConfig(
        slug="ordinary-self-edit",
        workspace_root=tmp_path / "workspace",
        results_root=tmp_path / "results",
        rounds=1,
        self_edit_every=1,
        self_edit_budget=1,
    )
    cfg.sandbox_dir.mkdir(parents=True)
    (cfg.sandbox_dir / "verify.sh").write_text("printf 'score=1\\n'\n")
    edit = _SelfEdit(cfg.sandbox_dir, cfg.results_dir, mutate_trajectory=False)

    outcome = await RsiLoop(
        cfg,
        engine=FakeEngine(script=[_score_one(cfg.results_dir)]),
        verifier=VerifierSpec(command="sh verify.sh", files=("verify.sh",)),
        self_edit=edit,
        cheat=CheatDetector(),
        problem="accept ordinary self-edit",
    ).run()

    state = read_trajectory(cfg.results_dir / "trajectory.json")
    assert outcome.exit_code == 0
    assert outcome.self_edits == 1
    assert state.best_score == 1.0
    assert state.events[-1].event == "self_edit"
    assert (cfg.sandbox_dir / SCAFFOLD_FILENAME).read_text() == "SELF EDIT\n"
