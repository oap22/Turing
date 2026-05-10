"""Tests for the worker-side ``vault_propose`` tool wrapper."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from turing.vault.proposer import VaultProposer
from turing.worker.tools.vault_propose import vault_propose

if TYPE_CHECKING:
    from pathlib import Path


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "init"], cwd=repo, check=True)


@pytest.fixture
def proposer(tmp_path: Path) -> VaultProposer:
    _init_repo(tmp_path)
    return VaultProposer(
        vault_root=tmp_path,
        cluster_identity=("turing-cluster", "vault@turing.local"),
    )


def test_returns_structured_dict(proposer: VaultProposer) -> None:
    result = vault_propose(
        proposer=proposer,
        frontmatter={
            "source": "x-worker",
            "task_id": "task-1",
            "specialty": "research-summarize",
            "confidence": 0.5,
            "critic_score": 0.5,
        },
        body="content",
        slug="note-1",
    )
    assert "path" in result
    assert "task_id" in result
    assert result["task_id"] == "task-1"


def test_invalid_frontmatter_returned_as_error(proposer: VaultProposer) -> None:
    result = vault_propose(
        proposer=proposer,
        frontmatter={"source": "x"},  # missing fields
        body="content",
        slug="note-1",
    )
    assert "error" in result
