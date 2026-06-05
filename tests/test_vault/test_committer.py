"""Tests for VaultCommitter — the write-side of ADR 0010 §4 AC#3.

Each curated promotion becomes exactly one vault commit, authored under the
coordinator identity, with a structured message that doubles as the
reward-signal audit trail. Drives a real on-disk git repo so the staging +
commit plumbing is exercised end-to-end.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from turing.vault.committer import VaultCommitter, promotion_commit_message

IDENTITY = ("turing-coordinator", "coordinator@turing.local")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": IDENTITY[0],
            "GIT_AUTHOR_EMAIL": IDENTITY[1],
            "GIT_COMMITTER_NAME": IDENTITY[0],
            "GIT_COMMITTER_EMAIL": IDENTITY[1],
        },
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "root")


def _committer(repo: Path, **kw: object) -> VaultCommitter:
    return VaultCommitter(vault_root=repo, identity=IDENTITY, **kw)  # type: ignore[arg-type]


# ── commit_paths ─────────────────────────────────────────────────────────────


def test_commit_paths_stages_and_commits_new_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "curated").mkdir()
    note = tmp_path / "curated" / "answer.md"
    note.write_text("# Answer\n\nbody\n", encoding="utf-8")

    sha = _committer(tmp_path).commit_paths(paths=[note], message="vault: curate accept t/answer")

    assert sha == _git(tmp_path, "rev-parse", "HEAD")
    # The new note is tracked at HEAD.
    tracked = _git(tmp_path, "ls-tree", "--name-only", "-r", "HEAD")
    assert "curated/answer.md" in tracked.splitlines()


def test_commit_paths_records_the_coordinator_identity(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    note = tmp_path / "n.md"
    note.write_text("x", encoding="utf-8")

    _committer(tmp_path).commit_paths(paths=[note], message="m")

    assert _git(tmp_path, "log", "-1", "--format=%an") == IDENTITY[0]
    assert _git(tmp_path, "log", "-1", "--format=%ae") == IDENTITY[1]


def test_commit_paths_stages_deletion_via_parent_dir(tmp_path: Path) -> None:
    # A tracked inbox draft, then removed on disk (the promotion moved it out).
    _init_repo(tmp_path)
    inbox = tmp_path / "vault" / "inbox" / "night-1"
    inbox.mkdir(parents=True)
    draft = inbox / "answer.md"
    draft.write_text("draft", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed inbox draft")

    curated = tmp_path / "curated" / "answer.md"
    curated.parent.mkdir()
    curated.write_text("draft", encoding="utf-8")
    draft.unlink()  # promotion removed the inbox copy

    _committer(tmp_path).commit_paths(paths=[curated, inbox], message="promote")

    tracked = _git(tmp_path, "ls-tree", "--name-only", "-r", "HEAD").splitlines()
    assert "curated/answer.md" in tracked
    assert "vault/inbox/night-1/answer.md" not in tracked  # deletion committed


def test_commit_paths_returns_none_when_nothing_staged(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    note = tmp_path / "n.md"
    note.write_text("x", encoding="utf-8")
    committer = _committer(tmp_path)
    committer.commit_paths(paths=[note], message="first")

    # Re-committing the same unchanged path stages nothing → idempotent no-op.
    assert committer.commit_paths(paths=[note], message="again") is None


def test_commit_paths_accepts_relative_pathspecs(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "curated").mkdir()
    (tmp_path / "curated" / "a.md").write_text("y", encoding="utf-8")

    sha = _committer(tmp_path).commit_paths(paths=[Path("curated/a.md")], message="rel")

    assert sha == _git(tmp_path, "rev-parse", "HEAD")


def test_push_sends_to_remote(tmp_path: Path) -> None:
    # A bare "remote" the committer pushes to after committing.
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare")

    work = tmp_path / "work"
    work.mkdir()
    _init_repo(work)
    _git(work, "branch", "-M", "main")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "main")

    note = work / "n.md"
    note.write_text("z", encoding="utf-8")
    sha = _committer(work, push=True).commit_paths(paths=[note], message="pushed")

    # The remote now has the committed sha on main.
    assert _git(remote, "rev-parse", "main") == sha


# ── promotion_commit_message ─────────────────────────────────────────────────


def test_promotion_commit_message_is_structured_and_grepable() -> None:
    msg = promotion_commit_message(
        decision="accept",
        task_id="night-1",
        slug="answer",
        specialty="ai-ml-generalist",
        episode_id="ep-1",
        reward=1.0,
        curated_rel="curated/ai-ml-generalist/answer.md",
    )

    lines = msg.splitlines()
    assert lines[0] == "vault: curate accept night-1/answer"
    assert "episode_id: ep-1" in lines
    assert "task_id: night-1" in lines
    assert "specialty: ai-ml-generalist" in lines
    assert "reward: +1.0" in lines
    assert "curated: curated/ai-ml-generalist/answer.md" in lines


def test_promotion_commit_message_signs_negative_reward() -> None:
    msg = promotion_commit_message(
        decision="edit",
        task_id="t",
        slug="s",
        specialty="x",
        episode_id="e",
        reward=0.3,
        curated_rel="curated/x/s.md",
    )
    assert "reward: +0.3" in msg.splitlines()
