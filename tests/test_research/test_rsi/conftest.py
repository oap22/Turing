"""Fixtures for the RSI workstation loop tests: tmp sandbox + results dirs, fake trajectories.

No subprocesses, no ``claude``. The contracts are properties of the types
and must hold with nothing else running.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from turing.research.rsi.contracts import RsiConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class RsiDirs:
    """A resolved sandbox/results pair rooted in ``tmp_path``."""

    config: RsiConfig
    sandbox: Path
    results: Path


@pytest.fixture
def rsi_dirs(tmp_path: Path) -> RsiDirs:
    config = RsiConfig(
        slug="demo",
        results_root=tmp_path / "results",
        workspace_root=tmp_path / "workspace",
    )
    config.sandbox_dir.mkdir(parents=True)
    config.results_dir.mkdir(parents=True)
    return RsiDirs(config=config, sandbox=config.sandbox_dir, results=config.results_dir)


@pytest.fixture
def sandbox(rsi_dirs: RsiDirs) -> Path:
    return rsi_dirs.sandbox


@pytest.fixture
def results(rsi_dirs: RsiDirs) -> Path:
    return rsi_dirs.results


@pytest.fixture
def write_trajectory(results: Path) -> Callable[[Iterable[Mapping[str, Any]]], Path]:
    """Append JSONL lines (round records or events) to ``<results>/trajectory.json``."""

    def _write(lines: Iterable[Mapping[str, Any]]) -> Path:
        path = results / "trajectory.json"
        with path.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        return path

    return _write


def bash_round(
    round_no: int, *, exit_code: int = 0, started: int = 1_700_000_000
) -> dict[str, Any]:
    """A line exactly as ``scripts/rsi-loop.sh`` writes it."""
    return {"round": round_no, "started": started, "ended": started + 60, "exit": exit_code}
