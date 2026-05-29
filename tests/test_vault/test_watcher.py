"""Tests for VaultWatcher — git-commit-driven incremental reindex (ADR 0010 §4).

Each commit diffs against its parent; only the changed markdown files mutate
the index (add/overwrite for added+modified, remove for deleted). These tests
drive a real on-disk git repo so the diff parsing is exercised end-to-end, and
assert on the resulting index state (`snapshot()`), which is the public surface
workers query against.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from turing.vault.embedder import DeterministicHashEmbedder
from turing.vault.index import VaultIndex
from turing.vault.watcher import CommitDiff, VaultWatcher

if TYPE_CHECKING:
    from pathlib import Path


# ── git helpers ──────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> None:
    env = {
        "GIT_AUTHOR_NAME": "turing-cluster",
        "GIT_AUTHOR_EMAIL": "vault@turing.local",
        "GIT_COMMITTER_NAME": "turing-cluster",
        "GIT_COMMITTER_EMAIL": "vault@turing.local",
    }
    import os

    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**os.environ, **env},
    )


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "vault@turing.local")
    _git(repo, "config", "user.name", "turing-cluster")


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _head(repo)


def _head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    return tmp_path


@pytest.fixture()
def index() -> VaultIndex:
    return VaultIndex(embedder=DeterministicHashEmbedder(dims=32))


@pytest.fixture()
def watcher(repo: Path, index: VaultIndex) -> VaultWatcher:
    return VaultWatcher(vault_root=repo, index=index)


# ── added files ──────────────────────────────────────────────────────────


def test_root_commit_indexes_whole_tree(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    _write(repo, "notes/python.md", "python async tips")
    _write(repo, "notes/cooking.md", "roast vegetables")
    sha = _commit_all(repo, "seed")

    diff = watcher.reindex_commit(sha)

    assert set(diff.added) == {"notes/python.md", "notes/cooking.md"}
    assert diff.modified == ()
    assert diff.deleted == ()
    assert set(index.snapshot()) == {"notes/python.md", "notes/cooking.md"}


def test_added_file_in_later_commit_is_indexed(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    _write(repo, "notes/a.md", "alpha")
    watcher.reindex_commit(_commit_all(repo, "first"))

    _write(repo, "notes/b.md", "bravo")
    diff = watcher.reindex_commit(_commit_all(repo, "add b"))

    assert diff.added == ("notes/b.md",)
    assert set(index.snapshot()) == {"notes/a.md", "notes/b.md"}


# ── modified files ─────────────────────────────────────────────────────────


def test_modified_file_overwrites_indexed_text(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    _write(repo, "notes/a.md", "old text")
    watcher.reindex_commit(_commit_all(repo, "first"))

    _write(repo, "notes/a.md", "brand new text")
    diff = watcher.reindex_commit(_commit_all(repo, "edit a"))

    assert diff.modified == ("notes/a.md",)
    assert diff.added == ()
    assert index.snapshot()["notes/a.md"] == "brand new text"


# ── deleted files ──────────────────────────────────────────────────────────


def test_deleted_file_is_removed_from_index(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    _write(repo, "notes/a.md", "alpha")
    _write(repo, "notes/b.md", "bravo")
    watcher.reindex_commit(_commit_all(repo, "first"))

    (repo / "notes" / "a.md").unlink()
    diff = watcher.reindex_commit(_commit_all(repo, "drop a"))

    assert diff.deleted == ("notes/a.md",)
    assert set(index.snapshot()) == {"notes/b.md"}


def test_mixed_commit_adds_modifies_and_deletes(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    _write(repo, "keep.md", "keep original")
    _write(repo, "gone.md", "delete me")
    watcher.reindex_commit(_commit_all(repo, "first"))

    _write(repo, "keep.md", "keep edited")
    (repo / "gone.md").unlink()
    _write(repo, "fresh.md", "newly added")
    diff = watcher.reindex_commit(_commit_all(repo, "churn"))

    assert diff.added == ("fresh.md",)
    assert diff.modified == ("keep.md",)
    assert diff.deleted == ("gone.md",)
    snap = index.snapshot()
    assert set(snap) == {"keep.md", "fresh.md"}
    assert snap["keep.md"] == "keep edited"


# ── renames ────────────────────────────────────────────────────────────────


def test_rename_removes_old_path_and_adds_new(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    _write(repo, "notes/old.md", "stable content that survives the move intact")
    watcher.reindex_commit(_commit_all(repo, "first"))

    _git(repo, "mv", "notes/old.md", "notes/new.md")
    diff = watcher.reindex_commit(_commit_all(repo, "rename"))

    assert "notes/old.md" in diff.deleted
    assert "notes/new.md" in diff.added
    assert set(index.snapshot()) == {"notes/new.md"}


# ── non-markdown is ignored ──────────────────────────────────────────────


def test_non_markdown_files_are_not_indexed(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    _write(repo, "notes/a.md", "alpha")
    _write(repo, "assets/diagram.png", "not really a png")
    _write(repo, ".gitignore", ".venv/\n")
    diff = watcher.reindex_commit(_commit_all(repo, "first"))

    assert diff.added == ("notes/a.md",)
    assert set(index.snapshot()) == {"notes/a.md"}


# ── idempotency / polling ──────────────────────────────────────────────────


def test_reindexing_same_commit_twice_is_a_noop(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    _write(repo, "notes/a.md", "alpha")
    sha = _commit_all(repo, "first")

    first = watcher.reindex_commit(sha)
    second = watcher.reindex_commit(sha)

    assert first.added == ("notes/a.md",)
    assert second.is_empty
    assert watcher.last_indexed_sha == sha


def test_poll_once_catches_up_to_head(repo: Path, index: VaultIndex, watcher: VaultWatcher) -> None:
    _write(repo, "notes/a.md", "alpha")
    sha = _commit_all(repo, "first")

    indexed = watcher.poll_once()

    assert indexed == sha
    assert set(index.snapshot()) == {"notes/a.md"}


def test_poll_once_is_idempotent_when_head_unchanged(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    _write(repo, "notes/a.md", "alpha")
    _commit_all(repo, "first")

    first = watcher.poll_once()
    # Mutate the index out-of-band; a no-op poll must NOT touch it again.
    index.remove_note(path="notes/a.md")
    second = watcher.poll_once()

    assert first == second
    assert index.snapshot() == {}  # not re-added — poll saw no new commit


def test_poll_once_applies_only_the_newest_commit_delta(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    _write(repo, "notes/a.md", "alpha")
    _commit_all(repo, "first")
    watcher.poll_once()

    _write(repo, "notes/b.md", "bravo")
    _commit_all(repo, "second")
    watcher.poll_once()

    assert set(index.snapshot()) == {"notes/a.md", "notes/b.md"}


# ── cold start with pre-existing history ─────────────────────────────────────


def test_cold_start_with_history_indexes_whole_tree(repo: Path, index: VaultIndex) -> None:
    """A fresh watcher on a clone that already has multiple commits must index
    the ENTIRE current tree on the first poll — not just the newest commit's
    changed files.

    This is the clone case: someone `git clone`s a vault with history, then the
    watcher starts cold (`last_indexed_sha is None`). Crucially this test does
    NOT poll per-commit while building the history — it commits a multi-commit
    history first, THEN starts the watcher and polls exactly once. A
    per-commit-poll test would mask the bug because the index would have been
    populated incrementally as each commit landed.
    """
    # Build several commits' worth of history with the watcher absent.
    _write(repo, "notes/python.md", "python async tips")
    _commit_all(repo, "first")
    _write(repo, "notes/cooking.md", "roast vegetables")
    _commit_all(repo, "second")
    _write(repo, "notes/python.md", "python async tips updated")
    _write(repo, "notes/travel.md", "kyoto in autumn")
    head = _commit_all(repo, "third")

    # Watcher starts cold, with no recorded cursor — exactly a fresh clone.
    watcher = VaultWatcher(vault_root=repo, index=index)
    indexed = watcher.poll_once()

    assert indexed == head
    # All three notes that exist at HEAD are indexed, including the one only
    # touched in the first commit (cooking was added in commit 2, python in 1).
    assert set(index.snapshot()) == {
        "notes/python.md",
        "notes/cooking.md",
        "notes/travel.md",
    }
    # And the indexed text reflects HEAD, not whatever an intermediate commit held.
    assert index.snapshot()["notes/python.md"] == "python async tips updated"


# ── batched (multi-commit) advance ───────────────────────────────────────────


def test_multi_commit_advance_indexes_all_changed_files(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    """When HEAD jumps several commits at once (a `git pull` landing a batch),
    a single poll must reindex EVERY commit's changes across the batch — not
    just HEAD^..HEAD, which would drop every intermediate commit's files.

    The watcher is caught up to the first commit, then THREE more commits land
    before the next poll. The test polls exactly once after the batch (no
    per-commit polling in between), which is what surfaces the bug.
    """
    _write(repo, "notes/a.md", "alpha")
    watcher.poll_once()  # cursor now at commit 1

    # Three commits land before the next poll (simulating a pull/fetch batch).
    _write(repo, "notes/b.md", "bravo")  # added in commit 2
    _commit_all(repo, "second")
    _write(repo, "notes/c.md", "charlie")  # added in commit 3
    (repo / "notes" / "a.md").unlink()  # a.md deleted in commit 3
    _commit_all(repo, "third")
    _write(repo, "notes/d.md", "delta")  # added in commit 4
    _write(repo, "notes/b.md", "bravo edited")  # b.md modified in commit 4
    head = _commit_all(repo, "fourth")

    indexed = watcher.poll_once()

    assert indexed == head
    # b/c/d all present (each added in a different intermediate commit),
    # a removed (deleted mid-batch). HEAD^..HEAD alone would have indexed only
    # d.md + the b.md edit and missed c.md entirely.
    snap = index.snapshot()
    assert set(snap) == {"notes/b.md", "notes/c.md", "notes/d.md"}
    assert snap["notes/b.md"] == "bravo edited"  # final state across the batch


def test_multi_commit_advance_from_resumed_cursor(repo: Path) -> None:
    """Same batch-advance guarantee starting from a persisted cursor (restart):
    a watcher resumed at an old SHA replays every commit up to HEAD on one poll.
    """
    _write(repo, "notes/a.md", "alpha")
    first = _commit_all(repo, "first")
    _write(repo, "notes/b.md", "bravo")
    _commit_all(repo, "second")
    _write(repo, "notes/c.md", "charlie")
    head = _commit_all(repo, "third")

    index = VaultIndex(embedder=DeterministicHashEmbedder(dims=32))
    watcher = VaultWatcher(vault_root=repo, index=index, last_indexed_sha=first)

    indexed = watcher.poll_once()

    assert indexed == head
    # b.md (commit 2) and c.md (commit 3) both land; a.md was already past the
    # cursor so it is not replayed but also not present (resumed past it).
    assert set(index.snapshot()) == {"notes/b.md", "notes/c.md"}


# ── empty repository ───────────────────────────────────────────────────────


def test_poll_on_empty_repo_does_not_crash(
    repo: Path, index: VaultIndex, watcher: VaultWatcher
) -> None:
    # Fresh repo, no commits at all.
    indexed = watcher.poll_once()

    assert indexed is None
    assert index.snapshot() == {}
    assert watcher.last_indexed_sha is None


# ── resume from a known SHA ──────────────────────────────────────────────


def test_watcher_resumes_from_last_indexed_sha(repo: Path) -> None:
    _write(repo, "notes/a.md", "alpha")
    first_sha = _commit_all(repo, "first")
    _write(repo, "notes/b.md", "bravo")
    second_sha = _commit_all(repo, "second")

    # A restarted watcher told it already applied `first_sha` should only pick
    # up the delta introduced by `second_sha`.
    index = VaultIndex(embedder=DeterministicHashEmbedder(dims=32))
    watcher = VaultWatcher(vault_root=repo, index=index, last_indexed_sha=first_sha)

    diff = watcher.reindex_commit(second_sha)

    assert diff.added == ("notes/b.md",)
    # `notes/a.md` was never replayed (resumed past it) — only the new file
    # lands. This mirrors a coordinator restart that persisted its cursor.
    assert set(index.snapshot()) == {"notes/b.md"}


# ── parser unit tests (no git) ─────────────────────────────────────────────


def test_parse_name_status_handles_each_status_code() -> None:
    raw = "A\x00added.md\x00M\x00changed.md\x00D\x00removed.md\x00A\x00ignored.txt\x00"
    diff = VaultWatcher._parse_name_status(raw)

    assert diff == CommitDiff(
        added=("added.md",),
        modified=("changed.md",),
        deleted=("removed.md",),
    )


def test_parse_name_status_handles_rename_record() -> None:
    raw = "R100\x00old/name.md\x00new/name.md\x00"
    diff = VaultWatcher._parse_name_status(raw)

    assert diff.deleted == ("old/name.md",)
    assert diff.added == ("new/name.md",)
