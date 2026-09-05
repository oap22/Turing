"""``python -m turing.research.rsi`` — dry-run output, usage errors, resume refusal, fake run.

Everything goes through :func:`turing.research.rsi.cli.main` with an argv
list, so these are the same code paths ``scripts/rsi-loop.sh`` execs into;
the last class runs the script itself to check the bridge.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from turing.research.rsi import cli
from turing.research.rsi.cheat import git_env
from turing.research.rsi.cli import (
    ALLOW_FAKE_ENGINE_ENV,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_STOPPED,
    EXIT_USAGE,
    count_round_lines,
    main,
)
from turing.research.rsi.contracts import (
    VERIFIER_LOCK_FILENAME,
    ContractViolationError,
    VerifierSpec,
)
from turing.research.rsi.taxonomy import TAXONOMY_DIGEST, TAXONOMY_FILENAME
from turing.research.rsi.verifier import write_or_load_verifier

from .conftest import RsiDirs, bash_round

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "rsi-loop.sh"


def _argv(dirs: RsiDirs, *extra: str) -> list[str]:
    cfg = dirs.config
    return [
        "--slug",
        cfg.slug,
        "--results-root",
        str(cfg.results_root),
        "--workspace-root",
        str(cfg.workspace_root),
        *extra,
    ]


def _round_lines(trajectory: Path) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in trajectory.read_text().splitlines() if line.strip()]
    return [line for line in lines if "event" not in line]


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd),
        env=git_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


class TestDryRun:
    def test_prints_plan_and_touches_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        results_root = tmp_path / "results"
        workspace = tmp_path / "ws"
        code = main(
            [
                "--slug",
                "fresh",
                "--results-root",
                str(results_root),
                "--workspace-root",
                str(workspace),
                "--verifier",
                "echo score=1",
                "--problem",
                "do a thing",
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert f"sandbox: {workspace / 'rsi-fresh'}" in out
        assert f"results: {results_root / 'loop-rsi-fresh'}" in out
        assert "rounds: 10" in out
        assert "resuming: no" in out
        assert "start round: 1" in out
        assert "verifier lock: absent (will be written" in out
        assert "pinned files: NONE" in out
        assert "REFUSED" not in out
        assert TAXONOMY_DIGEST in out
        assert f"{TAXONOMY_FILENAME} absent" in out
        # Nothing on disk.
        assert not results_root.exists()
        assert not workspace.exists()

    def test_reports_resume_and_intact_lock(
        self, rsi_dirs: RsiDirs, write_trajectory: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (rsi_dirs.sandbox / "PROBLEM.md").write_text("goal\n")
        write_or_load_verifier(VerifierSpec(command="echo score=1"), rsi_dirs.sandbox, now_ms=1)
        write_trajectory(
            [bash_round(1), {"event": "self_edit", "round": 1, "ts": 1}, bash_round(2)]
        )  # type: ignore[operator]
        code = main(_argv(rsi_dirs, "--dry-run", "--rounds", "0"))
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert "resuming: yes" in out
        assert "start round: 3" in out  # the event line is skipped
        assert "rounds: unlimited" in out
        assert "verifier lock: present, intact" in out
        assert "REFUSED" not in out

    def test_fake_engine_allowed_under_dry_run(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(ALLOW_FAKE_ENGINE_ENV, raising=False)
        code = main(_argv(rsi_dirs, "--engine", "fake", "--verifier", "true", "--dry-run"))
        assert code == EXIT_OK
        assert "engine: fake" in capsys.readouterr().out

    def test_dry_run_lists_every_refusal_the_run_would_apply(
        self, rsi_dirs: RsiDirs, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No lock, no --verifier, no --problem: the plan names both, exits 0, writes nothing.
        code = main(_argv(rsi_dirs, "--dry-run"))
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert "REFUSED (exit 2): --verifier is required on the first run" in out
        assert "REFUSED (exit 2): --problem is required on the first run" in out
        assert not (rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).exists()

    def test_relative_results_root_is_made_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        code = main(
            [
                "--slug",
                "rel",
                "--results-root",
                "out",
                "--workspace-root",
                "ws",
                "--verifier",
                "true",
                "--problem",
                "x",
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert f"results: {tmp_path / 'out' / 'loop-rsi-rel'}" in out
        assert f"sandbox: {tmp_path / 'ws' / 'rsi-rel'}" in out


# --------------------------------------------------------------------------- #
# Usage errors → exit 2
# --------------------------------------------------------------------------- #


class TestUsageErrors:
    def test_missing_slug(self, tmp_path: Path) -> None:
        assert main(["--results-root", str(tmp_path)]) == EXIT_USAGE

    def test_bad_slug(self, tmp_path: Path) -> None:
        assert main(["--slug", "Bad_Slug", "--results-root", str(tmp_path)]) == EXIT_USAGE

    def test_missing_results_root(self) -> None:
        assert main(["--slug", "ok"]) == EXIT_USAGE

    def test_negative_rounds(self, tmp_path: Path) -> None:
        assert (
            main(["--slug", "ok", "--results-root", str(tmp_path), "--rounds", "-1"]) == EXIT_USAGE
        )

    def test_unknown_flag(self, tmp_path: Path) -> None:
        assert main(["--slug", "ok", "--results-root", str(tmp_path), "--bogus"]) == EXIT_USAGE

    def test_fake_engine_refused_without_env(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(ALLOW_FAKE_ENGINE_ENV, raising=False)
        code = main(_argv(rsi_dirs, "--engine", "fake", "--verifier", "true", "--problem", "x"))
        assert code == EXIT_USAGE
        assert ALLOW_FAKE_ENGINE_ENV in capsys.readouterr().out
        assert not (rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).exists()

    def test_first_run_requires_verifier(
        self, rsi_dirs: RsiDirs, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(_argv(rsi_dirs, "--problem", "x"))
        assert code == EXIT_USAGE
        assert "--verifier is required on the first run" in capsys.readouterr().out
        assert not (rsi_dirs.sandbox / "PROBLEM.md").exists()

    def test_first_run_requires_problem(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        code = main(_argv(rsi_dirs, "--engine", "fake", "--verifier", "echo score=1"))
        assert code == EXIT_USAGE
        assert "--problem is required on the first run" in capsys.readouterr().out
        # Refused before the lock or taxonomy was written.
        assert not (rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).exists()
        assert not (rsi_dirs.results / TAXONOMY_FILENAME).exists()

    def test_verifier_file_without_verifier(
        self, rsi_dirs: RsiDirs, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(_argv(rsi_dirs, "--verifier-file", "grade.py", "--problem", "x"))
        assert code == EXIT_USAGE
        assert "--verifier-file requires --verifier" in capsys.readouterr().out

    def test_verifier_file_may_not_climb_out(
        self, rsi_dirs: RsiDirs, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            _argv(rsi_dirs, "--verifier", "true", "--verifier-file", "../x", "--problem", "x")
        )
        assert code == EXIT_USAGE
        assert "may not climb out" in capsys.readouterr().out

    def test_taxonomy_mismatch_refused_before_any_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        results_root = tmp_path / "results"
        workspace = tmp_path / "ws"
        results = results_root / "loop-rsi-tax"
        results.mkdir(parents=True)
        (results / TAXONOMY_FILENAME).write_text(
            json.dumps({"version": "0", "digest": "0" * 64, "categories": {}})
        )
        code = main(
            [
                "--slug",
                "tax",
                "--results-root",
                str(results_root),
                "--workspace-root",
                str(workspace),
                "--engine",
                "fake",
                "--verifier",
                "true",
                "--problem",
                "x",
            ]
        )
        assert code == EXIT_USAGE
        assert "taxonomy digest mismatch" in capsys.readouterr().out
        assert not workspace.exists()  # the sandbox was not created


# --------------------------------------------------------------------------- #
# Resume with a different verifier is refused loudly
# --------------------------------------------------------------------------- #


class TestResumeVerifier:
    def test_different_verifier_on_resume_refused(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        (rsi_dirs.sandbox / "PROBLEM.md").write_text("goal\n")
        lock, _ = write_or_load_verifier(
            VerifierSpec(command="echo score=1"), rsi_dirs.sandbox, now_ms=1
        )
        before = (rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).read_text()

        code = main(_argv(rsi_dirs, "--engine", "fake", "--verifier", "echo score=2"))
        out = capsys.readouterr().out
        assert code == EXIT_USAGE
        assert "--verifier differs from the locked verifier" in out
        # Never ignored, never overwritten.
        assert (rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).read_text() == before
        assert lock.command == "echo score=1"

    def test_different_verifier_reported_in_dry_run(
        self, rsi_dirs: RsiDirs, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_or_load_verifier(VerifierSpec(command="echo score=1"), rsi_dirs.sandbox, now_ms=1)
        code = main(_argv(rsi_dirs, "--verifier", "echo score=2", "--dry-run"))
        out = capsys.readouterr().out
        assert code == EXIT_OK
        # The lock itself is fine; it is the flag that differs.
        assert "verifier lock: present, intact, DIFFERS from --verifier" in out
        assert "BROKEN" not in out
        assert "REFUSED (exit 2): --verifier differs from the locked verifier" in out

    def test_different_verifier_files_on_resume_refused(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        (rsi_dirs.sandbox / "PROBLEM.md").write_text("goal\n")
        (rsi_dirs.sandbox / "extra.txt").write_text("x\n")
        write_or_load_verifier(VerifierSpec(command="echo score=1"), rsi_dirs.sandbox, now_ms=1)
        code = main(
            _argv(
                rsi_dirs,
                "--engine",
                "fake",
                "--verifier",
                "echo score=1",
                "--verifier-file",
                "extra.txt",
            )
        )
        assert code == EXIT_USAGE
        assert "--verifier-file differs from the locked verifier" in capsys.readouterr().out

    def test_tampered_pinned_file_exits_stopped(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        (rsi_dirs.sandbox / "PROBLEM.md").write_text("goal\n")
        script = rsi_dirs.sandbox / "grade.sh"
        script.write_text("echo score=1\n")
        write_or_load_verifier(
            VerifierSpec(command="sh grade.sh", files=("grade.sh",)), rsi_dirs.sandbox, now_ms=1
        )
        script.write_text("echo score=999\n")  # tamper while the loop is not running

        code = main(_argv(rsi_dirs, "--engine", "fake"))
        assert code == EXIT_STOPPED
        assert "verifier tampered" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# I1: a missing lock on a sandbox that already ran is never re-locked
# --------------------------------------------------------------------------- #


class TestMissingLockOnResume:
    def _first_run(self, rsi_dirs: RsiDirs) -> Path:
        script = rsi_dirs.sandbox / "grade.sh"
        script.write_text("echo score=1\n")
        code = main(
            _argv(
                rsi_dirs,
                "--engine",
                "fake",
                "--verifier",
                "./grade.sh",
                "--problem",
                "x",
                "--rounds",
                "1",
                "--self-edit-every",
                "0",
            )
        )
        assert code == EXIT_OK
        return rsi_dirs.results / "trajectory.json"

    def test_rerun_after_lock_removed_is_refused(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The reviewers' probe B: the agent rewrites the grader, `git rm`s the lock, commits."""
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        trajectory = self._first_run(rsi_dirs)
        (rsi_dirs.sandbox / "grade.sh").write_text("echo score=999\n")
        _git(rsi_dirs.sandbox, "rm", "-q", "--", VERIFIER_LOCK_FILENAME)
        _git(rsi_dirs.sandbox, "commit", "-q", "-am", "agent: drop the lock")
        before = trajectory.read_text()
        capsys.readouterr()

        # The operator's original command line, unchanged.
        code = main(
            _argv(
                rsi_dirs,
                "--engine",
                "fake",
                "--verifier",
                "./grade.sh",
                "--problem",
                "x",
                "--rounds",
                "1",
                "--self-edit-every",
                "0",
            )
        )
        out = capsys.readouterr().out
        assert code == EXIT_USAGE
        assert (
            f"{VERIFIER_LOCK_FILENAME} is missing from a sandbox that already ran 1 round(s)" in out
        )
        assert "git history in the sandbox tracked" in out
        assert not (rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).exists()  # never re-locked
        assert trajectory.read_text() == before  # nothing appended

    def test_problem_and_rounds_without_lock_is_refused_even_without_git_history(
        self,
        rsi_dirs: RsiDirs,
        monkeypatch: pytest.MonkeyPatch,
        write_trajectory: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Probe E: PROBLEM.md + trajectory but no lock and no git — still not a first run."""
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        (rsi_dirs.sandbox / "PROBLEM.md").write_text("goal\n")
        write_trajectory([bash_round(1), bash_round(2)])  # type: ignore[operator]
        code = main(_argv(rsi_dirs, "--engine", "fake", "--verifier", "echo score=5"))
        out = capsys.readouterr().out
        assert code == EXIT_USAGE
        assert "already ran 2 round(s)" in out
        assert "PROBLEM.md exists" in out
        assert not (rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).exists()

    def test_dry_run_reports_the_refusal_instead_of_a_fresh_lock(
        self, rsi_dirs: RsiDirs, write_trajectory: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (rsi_dirs.sandbox / "PROBLEM.md").write_text("goal\n")
        write_trajectory([bash_round(1)])  # type: ignore[operator]
        code = main(_argv(rsi_dirs, "--verifier", "echo score=5", "--dry-run"))
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert "verifier lock: absent, but this slug already ran" in out
        assert "will be written from --verifier" not in out
        assert "REFUSED (exit 2)" in out


# --------------------------------------------------------------------------- #
# I1: the CLI can pin files, and refuses a first run that would pin nothing
# --------------------------------------------------------------------------- #


class TestVerifierFilePinning:
    def test_python_grade_py_pins_nothing_and_is_refused(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The doc's old worked example: `python grade.py` froze only the command string."""
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        (rsi_dirs.sandbox / "grade.py").write_text("print('score=1')\n")
        code = main(
            _argv(rsi_dirs, "--engine", "fake", "--verifier", "python3 grade.py", "--problem", "x")
        )
        out = capsys.readouterr().out
        assert code == EXIT_USAGE
        assert "would pin NO file" in out
        assert "--verifier-file grade.py" in out
        assert not (rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).exists()

    def test_dry_run_shows_what_would_be_pinned(
        self, rsi_dirs: RsiDirs, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (rsi_dirs.sandbox / "grade.py").write_text("print('score=1')\n")
        code = main(
            _argv(
                rsi_dirs,
                "--verifier",
                "python3 grade.py",
                "--verifier-file",
                "grade.py",
                "--problem",
                "x",
                "--dry-run",
            )
        )
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert "pinned files: grade.py" in out
        assert "REFUSED" not in out
        assert not (rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).exists()

    def test_missing_verifier_file_is_refused(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        code = main(
            _argv(
                rsi_dirs,
                "--engine",
                "fake",
                "--verifier",
                "true",
                "--verifier-file",
                "nope.py",
                "--problem",
                "x",
            )
        )
        assert code == EXIT_USAGE
        assert "not a regular file in the sandbox" in capsys.readouterr().out

    def test_verifier_file_through_main_then_tamper_exits_stopped(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The operator's actual path: `--verifier 'sh grade.sh' --verifier-file grade.sh`."""
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        script = rsi_dirs.sandbox / "grade.sh"
        script.write_text("echo score=1\n")
        code = main(
            _argv(
                rsi_dirs,
                "--engine",
                "fake",
                "--verifier",
                "sh grade.sh",
                "--verifier-file",
                "grade.sh",
                "--problem",
                "x",
                "--rounds",
                "1",
                "--self-edit-every",
                "0",
            )
        )
        out = capsys.readouterr().out
        assert code == EXIT_OK, out
        assert "verifier lock will pin: grade.sh" in out
        lock = json.loads((rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).read_text())
        assert list(lock["file_sha256s"]) == ["grade.sh"]
        trajectory = rsi_dirs.results / "trajectory.json"
        assert _round_lines(trajectory)[0]["score"] == 1.0

        script.write_text("echo score=9\n")  # tamper while idle
        code = main(_argv(rsi_dirs, "--engine", "fake", "--rounds", "1", "--self-edit-every", "0"))
        out = capsys.readouterr().out
        assert code == EXIT_STOPPED
        assert "verifier tampered" in out
        # The inflated score was never recorded (I2), only the good round is.
        assert [r["score"] for r in _round_lines(trajectory)] == [1.0]


# --------------------------------------------------------------------------- #
# I7: a tamper found at start-up is on the trajectory, not only on stdout
# --------------------------------------------------------------------------- #


class TestStartupTamperEvent:
    def test_event_line_is_appended(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, write_trajectory: object
    ) -> None:
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        (rsi_dirs.sandbox / "PROBLEM.md").write_text("goal\n")
        script = rsi_dirs.sandbox / "grade.sh"
        script.write_text("echo score=1\n")
        write_or_load_verifier(VerifierSpec(command="./grade.sh"), rsi_dirs.sandbox, now_ms=1)
        trajectory = write_trajectory([bash_round(1), bash_round(2)])  # type: ignore[operator]
        before = trajectory.read_text()
        script.write_text("echo score=999\n")

        assert main(_argv(rsi_dirs, "--engine", "fake")) == EXIT_STOPPED
        after = trajectory.read_text()
        assert after.startswith(before)  # append-only
        lines = [json.loads(line) for line in after.splitlines() if line.strip()]
        last = lines[-1]
        assert last["event"] == "verifier_tampered"
        assert last["round"] == 3  # the round that would have run
        assert last["when"] == "startup"
        assert "grade.sh" in last["detail"]
        assert len(lines) == 3


# --------------------------------------------------------------------------- #
# Dry run and the real run agree on a corrupt trajectory
# --------------------------------------------------------------------------- #


class TestCorruptTrajectory:
    def test_dry_run_and_run_agree(
        self,
        rsi_dirs: RsiDirs,
        monkeypatch: pytest.MonkeyPatch,
        write_trajectory: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        (rsi_dirs.sandbox / "PROBLEM.md").write_text("goal\n")
        write_or_load_verifier(VerifierSpec(command="echo score=1"), rsi_dirs.sandbox, now_ms=1)
        trajectory = write_trajectory([bash_round(1)])  # type: ignore[operator]
        with trajectory.open("a") as fh:
            fh.write('{"round": 2, "started": 1, "ended')  # crash mid-append
        before = trajectory.read_text()

        assert main(_argv(rsi_dirs, "--dry-run")) == EXIT_OK
        out = capsys.readouterr().out
        assert "start round: UNKNOWN" in out
        assert "trajectory.json:2 is not JSON" in out
        assert "REFUSED (exit 2)" in out
        assert "truncate the partial last line" in out

        assert main(_argv(rsi_dirs, "--engine", "fake")) == EXIT_USAGE
        assert "trajectory.json:2 is not JSON" in capsys.readouterr().out
        assert trajectory.read_text() == before


# --------------------------------------------------------------------------- #
# Ctrl-C
# --------------------------------------------------------------------------- #


class TestInterrupt:
    def test_keyboard_interrupt_exits_130(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")

        def _boom(coro: object) -> int:
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[union-attr]
            raise KeyboardInterrupt

        monkeypatch.setattr(cli.asyncio, "run", _boom)
        code = main(_argv(rsi_dirs, "--engine", "fake", "--verifier", "true", "--problem", "x"))
        assert code == EXIT_INTERRUPTED == 130
        assert "interrupted: no trajectory line was written" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class TestCountRoundLines:
    def test_skips_events_and_blank_lines(
        self, rsi_dirs: RsiDirs, write_trajectory: object
    ) -> None:
        path = write_trajectory(  # type: ignore[operator]
            [bash_round(1), {"event": "rollback", "round": 1, "ts": 2, "reverted": "abc"}]
        )
        with path.open("a") as fh:
            fh.write("\n")
            fh.write(json.dumps(bash_round(2)) + "\n")
        assert count_round_lines(path) == 2

    def test_missing_file_is_zero(self, rsi_dirs: RsiDirs) -> None:
        assert count_round_lines(rsi_dirs.results / "trajectory.json") == 0

    def test_non_json_line_raises_like_the_run(self, rsi_dirs: RsiDirs) -> None:
        path = rsi_dirs.results / "trajectory.json"
        path.write_text(json.dumps(bash_round(1)) + "\nnot json\n")
        with pytest.raises(ContractViolationError):
            count_round_lines(path)


# --------------------------------------------------------------------------- #
# End to end through main() with the fake engine (depends on the loop module)
# --------------------------------------------------------------------------- #


class TestFakeEngineRun:
    def test_two_rounds_end_to_end(
        self, rsi_dirs: RsiDirs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(ALLOW_FAKE_ENGINE_ENV, "1")
        code = main(
            _argv(
                rsi_dirs,
                "--engine",
                "fake",
                "--verifier",
                "echo score=1",
                "--problem",
                "count to two",
                "--rounds",
                "2",
                "--self-edit-every",
                "0",
            )
        )
        out = capsys.readouterr().out
        assert code == EXIT_OK, out
        assert "verifier lock will pin: NOTHING" in out
        assert (rsi_dirs.sandbox / "PROBLEM.md").read_text() == "count to two\n"
        assert (rsi_dirs.sandbox / VERIFIER_LOCK_FILENAME).exists()
        assert (
            json.loads((rsi_dirs.results / TAXONOMY_FILENAME).read_text())["digest"]
            == TAXONOMY_DIGEST
        )

        trajectory = rsi_dirs.results / "trajectory.json"
        rounds = _round_lines(trajectory)
        assert [r["round"] for r in rounds] == [1, 2]
        for record in rounds:
            # Bash-compatible keys first (I7), loop-measured score (I2).
            assert list(record)[:4] == ["round", "started", "ended", "exit"]
            assert record["score"] == 1.0
            assert record["passed"] is True
        assert "done: 2 round(s)" in out

        # Resume: the third round continues the numbering; --verifier may be omitted.
        code = main(_argv(rsi_dirs, "--engine", "fake", "--rounds", "1", "--self-edit-every", "0"))
        assert code == EXIT_OK
        assert _round_lines(trajectory)[-1]["round"] == 3


# --------------------------------------------------------------------------- #
# The bash bridge itself
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
class TestBashBridge:
    """Runs ``scripts/rsi-loop.sh`` with ``HOME`` pointed at ``tmp_path``.

    The script prefers the repo's ``.venv`` interpreter; when that is absent it
    honours ``TURING_RSI_PYTHON``, which we set to the running interpreter.
    """

    def _run(self, home: Path, cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
        full_env = dict(os.environ)
        full_env.update({"HOME": str(home), "TURING_RSI_PYTHON": sys.executable})
        full_env.pop("TURING_RSI_ENGINE", None)
        if env:
            full_env.update(env)
        proc = subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=str(cwd),
            env=full_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout

    def test_relative_results_root_is_anchored_to_the_invocation_cwd(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        home.mkdir()
        out = self._run(
            home,
            elsewhere,
            "--slug",
            "relcheck",
            "--results-root",
            "out",
            "--verifier",
            "true",
            "--problem",
            "x",
            "--dry-run",
        )
        assert f"results: {elsewhere / 'out' / 'loop-rsi-relcheck'}" in out
        assert str(REPO_ROOT / "out") not in out
        # The bash path anchors the same way.
        out = self._run(home, elsewhere, "--slug", "relcheck", "--results-root", "out", "--dry-run")
        assert f"results: {elsewhere / 'out' / 'loop-rsi-relcheck'}" in out

    def test_locked_sandbox_routes_to_python_without_verifier_flag(self, tmp_path: Path) -> None:
        """The desktop's argv (no --verifier) on a slug the Python engine has locked."""
        home = tmp_path / "home"
        sandbox = home / "turing-workspace" / "rsi-locked"
        sandbox.mkdir(parents=True)
        (sandbox / "PROBLEM.md").write_text("goal\n")
        write_or_load_verifier(VerifierSpec(command="echo score=1"), sandbox, now_ms=1)
        out = self._run(
            home,
            tmp_path,
            "--slug",
            "locked",
            "--results-root",
            str(tmp_path / "res"),
            "--rounds",
            "10",
            "--problem",
            "goal",
            "--dry-run",
        )
        assert "verifier lock: present, intact" in out  # the Python plan, not the 4-line bash one
        assert "engine: claude" in out

    def test_unlocked_sandbox_keeps_the_bash_path(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        out = self._run(
            home,
            tmp_path,
            "--slug",
            "plain",
            "--results-root",
            str(tmp_path / "res"),
            "--problem",
            "goal",
            "--dry-run",
        )
        assert out.splitlines() == [
            f"sandbox: {home / 'turing-workspace' / 'rsi-plain'}",
            f"results: {tmp_path / 'res' / 'loop-rsi-plain'}",
            "rounds: 10",
            "resuming: no",
        ]
