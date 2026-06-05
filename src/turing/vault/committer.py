"""VaultCommitter — turns a curated promotion into one vault git commit.

ADR 0010 §4 AC#3 (the write-side): *the coordinator commits on curated
promotions, so the vault commit log is the reward-signal audit trail*
(CONTEXT.md "Vault"). This is the counterpart to :class:`VaultWatcher` — the
watcher is the read-side that reindexes by diffing each new commit, and this is
the write-side that produces exactly **one commit per promotion**, so the
watcher's reindex boundary lines up with the operator's curation boundary.

Where it runs: the Phase 0 curation surface (:class:`~turing.coordinator.
flywheel.morning_curation.MorningCuration`) injects a committer so that an
``accept``/``edit`` decision both moves the draft into the curated tree *and*
records that move as a structured, attributable commit. The same component is
reusable by the webui queue/chat curation surfaces (ADR 0010 Slice C/E).

Single-writer invariant (see :mod:`turing.vault.watcher`): the coordinator's
git is the only writer to the WSL2 working tree, which is exactly what makes a
"stage everything in the affected subtrees, then commit" model safe — there is
never a concurrent writer leaving unrelated changes staged.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class VaultCommitter:
    """Commits curated promotions to the vault repository under one identity.

    ``identity`` is ``(name, email)`` used as both the git author and committer,
    mirroring :class:`~turing.vault.proposer.VaultProposer` so the commit log
    has a single, recognisable cluster/coordinator author. When ``push`` is
    true the new commit is pushed to ``remote``/``branch`` after it lands (the
    Mac and other devices are pull-only mirrors — see
    ``docs/operator/vault-git-workflow.md``).
    """

    def __init__(
        self,
        *,
        vault_root: Path,
        identity: tuple[str, str],
        push: bool = False,
        remote: str = "origin",
        branch: str = "main",
    ) -> None:
        self._root = Path(vault_root).resolve()
        self._name, self._email = identity
        self._push = push
        self._remote = remote
        self._branch = branch

    def commit_paths(self, *, paths: Sequence[Path], message: str) -> str | None:
        """Stage adds + deletions under ``paths`` and commit them as one commit.

        Each entry in ``paths`` is staged with ``git add -A -- <path>`` so a
        promotion (a *new* curated file plus the *removed* inbox draft) collapses
        into a single commit. Passing a directory (e.g. the inbox draft's parent)
        captures the deletion of a now-removed draft without having to name a
        path that no longer exists on disk.

        Returns the new ``HEAD`` sha, or ``None`` when nothing was staged — that
        makes a re-promotion of an already-committed file an idempotent no-op
        rather than an empty-commit error.
        """
        rels = [self._rel(p) for p in paths]
        self._git("add", "-A", "--", *rels)
        if self._nothing_staged():
            return None
        self._commit(message)
        sha = self._head()
        if self._push:
            self._git("push", self._remote, self._branch)
        return sha

    # ── git plumbing ─────────────────────────────────────────────────────

    def _rel(self, path: Path) -> str:
        """Vault-relative POSIX path for a git pathspec (accepts abs or rel)."""
        p = Path(path)
        if p.is_absolute():
            p = p.resolve().relative_to(self._root)
        return p.as_posix()

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self._root, check=True, capture_output=True)

    def _nothing_staged(self) -> bool:
        # `git diff --cached --quiet` exits 1 when there ARE staged changes.
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=self._root,
            check=False,
            capture_output=True,
        )
        return result.returncode == 0

    def _commit(self, message: str) -> None:
        subprocess.run(
            ["git", "commit", "-q", "-m", message],
            cwd=self._root,
            check=True,
            capture_output=True,
            env=self._identity_env(),
        )

    def _head(self) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self._root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _identity_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "GIT_AUTHOR_NAME": self._name,
            "GIT_AUTHOR_EMAIL": self._email,
            "GIT_COMMITTER_NAME": self._name,
            "GIT_COMMITTER_EMAIL": self._email,
        }


def promotion_commit_message(
    *,
    decision: str,
    task_id: str,
    slug: str,
    specialty: str,
    episode_id: str,
    reward: float,
    curated_rel: str,
) -> str:
    """Structured, grep-able commit message for a curated promotion.

    The body is a flat ``key: value`` block so the commit log doubles as the
    reward-signal audit trail — ``git log`` reconstructs, per promotion, which
    episode earned which reward and where the curated note landed.
    """
    return "\n".join(
        [
            f"vault: curate {decision} {task_id}/{slug}",
            "",
            f"decision: {decision}",
            f"episode_id: {episode_id}",
            f"task_id: {task_id}",
            f"specialty: {specialty}",
            f"reward: {reward:+.1f}",
            f"curated: {curated_rel}",
        ]
    )
