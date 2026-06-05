"""VaultWatcher — git-commit-driven incremental reindex of the vault.

ADR 0010 §4: the vault is a git repository. The coordinator commits on curated
promotions, so the commit log *is* the reward-signal audit trail. Rather than
watching the filesystem and reindexing on every inotify event, the watcher
keys off literal git commits: on each new commit it diffs against the previous
commit and reindexes **only the changed files** —

* added / modified markdown → :meth:`VaultIndex.add_note` (overwrites in place)
* deleted / renamed-away markdown → :meth:`VaultIndex.remove_note`

This keeps the index O(changed-files) per commit instead of O(whole-tree), and
makes the reindex boundary exactly the operator's curation boundary (a commit).

Single-writer invariant: the coordinator's git is the *only* writer to the
WSL2 working tree. External Obsidian Sync of the same vault is incompatible —
see ``docs/operator/vault-git-workflow.md``.

Scope note — this watcher is the **read-side**: it consumes whatever the git
log already contains. The **write-side** of ADR 0010 §4 AC#3 (turning a
curation "accept"/"edit" into a vault commit) lives in
:class:`turing.vault.committer.VaultCommitter`, which the Phase 0 curation
surface (:class:`turing.coordinator.flywheel.morning_curation.MorningCuration`)
injects so each promotion is exactly one commit; the webui queue/chat surfaces
(Slice C/E) reuse the same committer. The single-writer invariant above is
exactly what makes that split safe: only one component ever writes the working
tree.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from turing.vault.index import VaultIndex

# Only markdown notes participate in the vector index. Other files (images,
# attachments, .gitignore, etc.) are committed but never embedded.
_MARKDOWN_SUFFIX = ".md"
# git diff status letters we care about. Renames/copies are split by
# --name-status into their constituent paths via the Rxxx/Cxxx prefixes.
_NULL = "\x00"


@dataclass(frozen=True)
class CommitDiff:
    """The set of vault-relative paths a single commit changed.

    Paths are repository-relative POSIX strings (git's native form), matching
    the keys :class:`VaultIndex` stores. ``added`` and ``modified`` are folded
    together at apply time — both resolve to ``add_note`` (an overwrite) — but
    kept distinct here so the diff is faithful for logging/tests.
    """

    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted)


class VaultWatcher:
    """Drives incremental reindexing from the vault's git commit log.

    Construct with the vault working-tree root and a populated
    :class:`VaultIndex`. Call :meth:`reindex_commit` for a specific SHA, or
    :meth:`poll_once` to catch the index up to whatever ``HEAD`` now points at.
    The watcher tracks the last SHA it has applied so re-polling the same
    commit is a no-op (idempotent).
    """

    def __init__(
        self,
        *,
        vault_root: Path,
        index: VaultIndex,
        last_indexed_sha: str | None = None,
    ) -> None:
        self._root = Path(vault_root).resolve()
        self._index = index
        self._last_indexed_sha = last_indexed_sha

    @property
    def last_indexed_sha(self) -> str | None:
        return self._last_indexed_sha

    # ── public API ───────────────────────────────────────────────────────

    def poll_once(self) -> str | None:
        """Catch the index up to ``HEAD``; return the SHA now indexed (or None).

        Two catch-up regimes, both keyed off how far behind the cursor is:

        * **Cold start with no cursor** (``last_indexed_sha is None``): the
          index is empty and the working tree may carry arbitrary history (a
          fresh clone of a repo with N commits). Index the *whole current
          tree* in one pass by diffing the empty tree against ``HEAD`` — not
          ``HEAD^..HEAD``, which would index only the newest commit's files
          and leave every older note unindexed.
        * **Advance from a known cursor**: ``HEAD`` may have jumped several
          commits (a ``git pull`` landing a batch). Replay *every* commit in
          ``last_indexed..HEAD`` in author order so no intermediate commit's
          changes are dropped — applying only ``HEAD``'s parent-diff would
          skip the files touched solely by the intervening commits.

        If ``HEAD`` is unchanged since the last applied commit this is a no-op
        and returns the unchanged SHA. On an empty repository (no commits yet)
        it returns ``None`` without touching the index.
        """
        head = self._head_sha()
        if head is None:
            return self._last_indexed_sha
        if head == self._last_indexed_sha:
            return head

        if self._last_indexed_sha is None:
            # Cold start: snapshot the entire tree at HEAD in a single diff.
            self._apply(self._diff_against(_EMPTY_TREE, head))
            self._last_indexed_sha = head
            return head

        # Cursor advanced (possibly by more than one commit). Replay each
        # commit's parent-diff in order so a batch fetch indexes every commit.
        for sha in self._commits_between(self._last_indexed_sha, head):
            self.reindex_commit(sha)
        return head

    def reindex_commit(self, sha: str) -> CommitDiff:
        """Diff ``sha`` against its parent and apply the changes to the index.

        Returns the :class:`CommitDiff` that was applied. The root commit (no
        parent) is diffed against the empty tree, so the whole tree is treated
        as added — this is the one-time bootstrap path. Re-applying the SHA the
        watcher already holds is a no-op.
        """
        if sha == self._last_indexed_sha:
            return CommitDiff(added=(), modified=(), deleted=())
        diff = self._diff_against_parent(sha)
        self._apply(diff)
        self._last_indexed_sha = sha
        return diff

    # ── index mutation ───────────────────────────────────────────────────

    def _apply(self, diff: CommitDiff) -> None:
        # Deletions first so a path that was deleted-then-readded in the same
        # batch (shouldn't happen within one commit, but be defensive) ends up
        # present rather than absent.
        for path in diff.deleted:
            self._index.remove_note(path=path)
        for path in (*diff.added, *diff.modified):
            text = self._read_note(path)
            if text is None:
                # File named in the diff is gone from the working tree (e.g. a
                # follow-on local edit). Treat as a removal rather than crash.
                self._index.remove_note(path=path)
            else:
                self._index.add_note(path=path, text=text)

    def _read_note(self, rel_path: str) -> str | None:
        abs_path = self._root / rel_path
        try:
            return abs_path.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError):
            return None

    # ── git plumbing ─────────────────────────────────────────────────────

    def _head_sha(self) -> str | None:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=self._root,
            capture_output=True,
            text=True,
            check=False,
        )
        sha = result.stdout.strip()
        return sha or None

    def _has_parent(self, sha: str) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{sha}^"],
            cwd=self._root,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def _commits_between(self, base: str, head: str) -> list[str]:
        """List commit SHAs in ``base..head``, oldest first.

        ``git rev-list --reverse base..head`` excludes ``base`` itself and
        yields the new commits in apply order, so a multi-commit advance
        (e.g. a ``git pull`` that fast-forwards several commits) is replayed
        commit-by-commit rather than collapsing to a single ``HEAD`` diff.
        """
        result = subprocess.run(
            ["git", "rev-list", "--reverse", f"{base}..{head}"],
            cwd=self._root,
            capture_output=True,
            text=True,
            check=True,
        )
        return [line for line in result.stdout.splitlines() if line]

    def _diff_against_parent(self, sha: str) -> CommitDiff:
        """Parse ``git diff --name-status`` for one commit into a CommitDiff.

        Root commits (no parent) diff against the empty-tree object so every
        file shows up as added. ``-z`` gives NUL-delimited records so paths
        with spaces or unicode survive intact; renames/copies emit a third NUL
        field (the new path) which we read explicitly.
        """
        base = f"{sha}^" if self._has_parent(sha) else _EMPTY_TREE
        return self._diff_against(base, sha)

    def _diff_against(self, base: str, head: str) -> CommitDiff:
        """Parse ``git diff --name-status base head`` into a :class:`CommitDiff`.

        ``-z`` gives NUL-delimited records so paths with spaces or unicode
        survive intact; renames/copies emit a third NUL field (the new path)
        which the parser reads explicitly. Diffing :data:`_EMPTY_TREE` against
        a commit yields its whole tree as additions — the cold-start path.
        """
        result = subprocess.run(
            ["git", "diff", "--name-status", "-z", base, head],
            cwd=self._root,
            capture_output=True,
            text=True,
            check=True,
        )
        return self._parse_name_status(result.stdout)

    @staticmethod
    def _parse_name_status(raw: str) -> CommitDiff:
        added: list[str] = []
        modified: list[str] = []
        deleted: list[str] = []

        fields = [f for f in raw.split(_NULL) if f != ""]
        i = 0
        while i < len(fields):
            status = fields[i]
            code = status[0]
            if code in ("R", "C"):
                # Rename/copy: status, old-path, new-path. The old note vanishes
                # from its key; the new path is an add. Copies keep the source.
                old_path = fields[i + 1] if i + 1 < len(fields) else ""
                new_path = fields[i + 2] if i + 2 < len(fields) else ""
                i += 3
                if code == "R" and _is_markdown(old_path):
                    deleted.append(old_path)
                if _is_markdown(new_path):
                    added.append(new_path)
                continue
            path = fields[i + 1] if i + 1 < len(fields) else ""
            i += 2
            if not _is_markdown(path):
                continue
            if code == "A":
                added.append(path)
            elif code == "D":
                deleted.append(path)
            else:
                # M (modified), T (type-change), and anything else map to an
                # in-place overwrite of the indexed note.
                modified.append(path)

        return CommitDiff(
            added=_dedupe(added),
            modified=_dedupe(modified),
            deleted=_dedupe(deleted),
        )


# The well-known git empty-tree object — diffing a root commit against it
# yields the whole tree as additions without a special-case parser.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _is_markdown(path: str) -> bool:
    return path.endswith(_MARKDOWN_SUFFIX)


def _dedupe(paths: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for p in paths:
        seen.setdefault(p, None)
    return tuple(seen)
