"""Regression probes for the bounded v2 campaign launch guard."""

from __future__ import annotations

import json
import subprocess
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType


LAUNCHER_PATH = Path(__file__).parents[3] / "research/records/rsi-live-450-v2/run_live.py"


def _load_launcher() -> ModuleType:
    spec = spec_from_file_location("rsi_live_450_v2_launcher", LAUNCHER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load launcher at {LAUNCHER_PATH}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=RSI test",
            "-c",
            "user.email=rsi-test@example.invalid",
            "commit",
            "-qm",
            message,
        ],
        cwd=repo,
        check=True,
    )
    return _git(repo, "rev-parse", "HEAD")


def _candidate_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "candidate"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "src/marker.py").write_text("seed = 1\n")
    return repo, _commit(repo, "seed")


def test_launcher_refuses_wrong_candidate_seed(tmp_path: Path) -> None:
    launcher = _load_launcher()
    repo, seed_sha = _candidate_repo(tmp_path)
    (repo / "src/marker.py").write_text("seed = 2\n")
    _commit(repo, "wrong candidate")

    with pytest.raises(RuntimeError, match="seed mismatch"):
        launcher.validate_candidate_start(repo, seed_sha)


def test_launcher_refuses_dirty_candidate_source(tmp_path: Path) -> None:
    launcher = _load_launcher()
    repo, seed_sha = _candidate_repo(tmp_path)
    (repo / "src/marker.py").write_text("seed = changed\n")

    with pytest.raises(RuntimeError, match="does not match the committed seed"):
        launcher.validate_candidate_start(repo, seed_sha)


def test_launcher_main_refuses_ignored_source_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _load_launcher()
    repo, _ = _candidate_repo(tmp_path)
    (repo / ".gitignore").write_text("src/ignored.py\n")
    seed_sha = _commit(repo, "seed with ignore rule")
    (repo / "src/ignored.py").write_text("ignored = True\n")
    launch_config = tmp_path / "launch-config.json"
    launch_config.write_text(json.dumps({"seed_candidate": seed_sha}))
    monkeypatch.setattr(launcher, "CANDIDATE", repo)
    monkeypatch.setattr(launcher, "LAUNCH_CONFIG", launch_config)
    monkeypatch.setenv("RESEARCH_RUN_DIR", str(tmp_path / "run"))

    real_run = launcher.subprocess.run

    def unexpected_launch(*args: object, **kwargs: object) -> object:
        command = args[0] if args else kwargs.get("args")
        if (
            isinstance(command, (list, tuple))
            and command
            and str(command[0]).endswith("scripts/rsi-loop.sh")
        ):
            raise AssertionError(f"launch subprocess should not run: {args!r} {kwargs!r}")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(launcher.subprocess, "run", unexpected_launch)
    with pytest.raises(RuntimeError, match=r"ignored\.py"):
        launcher.main()


def test_launcher_main_accepts_pristine_seed_before_stub_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _load_launcher()
    repo, seed_sha = _candidate_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / "problem-v2.txt").write_text("problem\n")
    results = tmp_path / "results"
    run = tmp_path / "run"
    run.mkdir()
    launch_config = tmp_path / "launch-config.json"
    launch_config.write_text(json.dumps({"seed_candidate": seed_sha}))
    monkeypatch.setattr(launcher, "CANDIDATE", repo)
    monkeypatch.setattr(launcher, "STATE", state)
    monkeypatch.setattr(launcher, "RESULTS", results)
    monkeypatch.setattr(launcher, "LAUNCH_CONFIG", launch_config)
    monkeypatch.setenv("RESEARCH_RUN_DIR", str(run))

    real_run = launcher.subprocess.run
    launches: list[object] = []

    def stub_launch(*args: object, **kwargs: object) -> object:
        command = args[0] if args else kwargs.get("args")
        if (
            isinstance(command, (list, tuple))
            and command
            and str(command[0]).endswith("scripts/rsi-loop.sh")
        ):
            launches.append(command)
            return SimpleNamespace(returncode=0)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(launcher.subprocess, "run", stub_launch)
    assert launcher.main() == 1  # no trajectory round was fabricated by the stub
    assert len(launches) == 1
    metrics = json.loads((run / "metrics.json").read_text())
    assert metrics["candidate_source_changed"] is False
    assert metrics["candidate_source_before_sha256"] == metrics["candidate_source_after_sha256"]


def test_ignored_source_changes_physical_identity(tmp_path: Path) -> None:
    launcher = _load_launcher()
    repo, _ = _candidate_repo(tmp_path)
    (repo / ".gitignore").write_text("src/ignored.py\n")
    _commit(repo, "seed with ignore rule")
    before = launcher.source_tree_identity(repo)
    ignored = repo / "src/ignored.py"
    ignored.write_text("ignored = 1\n")
    introduced = launcher.source_tree_identity(repo)
    ignored.write_text("ignored = 2\n")
    mutated = launcher.source_tree_identity(repo)

    assert before != introduced
    assert introduced != mutated
