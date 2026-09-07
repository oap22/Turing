"""Regression probes for the bounded v2 campaign launch guard."""

from __future__ import annotations

import subprocess
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
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
    subprocess.run(["git", "add", "src", "tests"], cwd=repo, check=True)
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

    with pytest.raises(RuntimeError, match="dirty before launch"):
        launcher.validate_candidate_start(repo, seed_sha)
