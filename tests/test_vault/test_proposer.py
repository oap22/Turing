"""Tests for VaultProposer — workers writing to vault/inbox/ via git audit trail."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from turing.vault.proposer import (
    InvalidFrontmatterError,
    InvalidProposalPathError,
    VaultProposer,
)


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "vault@turing.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "turing-cluster"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _proposer(repo: Path) -> VaultProposer:
    _init_repo(repo)
    return VaultProposer(
        vault_root=repo,
        cluster_identity=("turing-cluster", "vault@turing.local"),
    )


def _frontmatter() -> dict[str, object]:
    return {
        "source": "research-summarize-worker",
        "task_id": "task-001",
        "specialty": "research-summarize",
        "confidence": 0.82,
        "critic_score": 0.71,
    }


class TestSuccessfulPropose:
    def test_writes_to_inbox_under_task_id(self, tmp_path: Path) -> None:
        proposer = _proposer(tmp_path)
        path = proposer.propose(
            frontmatter=_frontmatter(),
            body="# Note\nbody text",
            slug="my-note",
        )
        assert path.is_relative_to(tmp_path / "vault" / "inbox" / "task-001")
        assert path.suffix == ".md"
        assert path.read_text(encoding="utf-8").startswith("---\n")

    def test_slug_appears_in_filename(self, tmp_path: Path) -> None:
        proposer = _proposer(tmp_path)
        path = proposer.propose(
            frontmatter=_frontmatter(),
            body="content",
            slug="research-on-bose-einstein-condensates",
        )
        assert "research-on-bose-einstein-condensates" in path.name

    def test_creates_git_commit_authored_by_cluster_identity(
        self, tmp_path: Path
    ) -> None:
        proposer = _proposer(tmp_path)
        proposer.propose(frontmatter=_frontmatter(), body="x", slug="n1")

        author = subprocess.check_output(
            ["git", "log", "-1", "--pretty=format:%an <%ae>"], cwd=tmp_path
        ).decode("utf-8")
        assert author == "turing-cluster <vault@turing.local>"

    def test_commit_message_references_task_id(self, tmp_path: Path) -> None:
        proposer = _proposer(tmp_path)
        proposer.propose(frontmatter=_frontmatter(), body="x", slug="n1")
        msg = subprocess.check_output(
            ["git", "log", "-1", "--pretty=format:%s"], cwd=tmp_path
        ).decode("utf-8")
        assert "task-001" in msg

    def test_frontmatter_serialized_to_yaml(self, tmp_path: Path) -> None:
        proposer = _proposer(tmp_path)
        path = proposer.propose(
            frontmatter=_frontmatter(), body="hello", slug="my-note"
        )
        text = path.read_text(encoding="utf-8")
        assert "source: research-summarize-worker" in text
        assert "specialty: research-summarize" in text
        assert "task_id: task-001" in text


class TestFrontmatterValidation:
    @pytest.mark.parametrize(
        "missing", ["source", "task_id", "specialty", "confidence", "critic_score"]
    )
    def test_missing_required_field_rejected(
        self, tmp_path: Path, missing: str
    ) -> None:
        proposer = _proposer(tmp_path)
        fm = _frontmatter()
        del fm[missing]
        with pytest.raises(InvalidFrontmatterError, match=missing):
            proposer.propose(frontmatter=fm, body="x", slug="n1")

    def test_confidence_must_be_in_zero_to_one_range(self, tmp_path: Path) -> None:
        proposer = _proposer(tmp_path)
        fm = _frontmatter()
        fm["confidence"] = 1.5
        with pytest.raises(InvalidFrontmatterError, match="confidence"):
            proposer.propose(frontmatter=fm, body="x", slug="n1")

    def test_critic_score_must_be_in_zero_to_one_range(self, tmp_path: Path) -> None:
        proposer = _proposer(tmp_path)
        fm = _frontmatter()
        fm["critic_score"] = -0.1
        with pytest.raises(InvalidFrontmatterError, match="critic_score"):
            proposer.propose(frontmatter=fm, body="x", slug="n1")


class TestPathSafety:
    def test_slug_with_traversal_rejected(self, tmp_path: Path) -> None:
        proposer = _proposer(tmp_path)
        with pytest.raises(InvalidProposalPathError):
            proposer.propose(
                frontmatter=_frontmatter(), body="x", slug="../escape"
            )

    def test_task_id_with_traversal_rejected(self, tmp_path: Path) -> None:
        proposer = _proposer(tmp_path)
        fm = _frontmatter()
        fm["task_id"] = "../etc/passwd"
        with pytest.raises(InvalidProposalPathError):
            proposer.propose(frontmatter=fm, body="x", slug="n1")

    def test_curated_path_is_never_writeable(self, tmp_path: Path) -> None:
        """The proposer's only writeable path is vault/inbox/**."""
        proposer = _proposer(tmp_path)
        fm = _frontmatter()
        # Even with a malicious slug containing slashes, the resolved path
        # must remain inside vault/inbox/<task_id>/.
        with pytest.raises(InvalidProposalPathError):
            proposer.propose(frontmatter=fm, body="x", slug="../../curated/note")


class TestSlug:
    def test_slug_disallows_empty(self, tmp_path: Path) -> None:
        proposer = _proposer(tmp_path)
        with pytest.raises(InvalidProposalPathError):
            proposer.propose(frontmatter=_frontmatter(), body="x", slug="")
