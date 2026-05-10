"""VaultProposer — workers write to ``vault/inbox/<task_id>/<slug>.md``.

Every proposal is committed to the vault repository as the cluster identity,
giving the operator a git-native audit trail of what the cluster has
contributed and what was later promoted from inbox to the curated tree.

Path safety is non-negotiable: the curated tree is read-only from the
worker's perspective, so this module's only writeable target is
``vault/inbox/**``. Any frontmatter or slug that would resolve outside
that subtree is rejected before touching the disk.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

REQUIRED_FRONTMATTER_FIELDS = (
    "source",
    "task_id",
    "specialty",
    "confidence",
    "critic_score",
)
_INBOX_SUBDIR = ("vault", "inbox")
_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class InvalidFrontmatterError(ValueError):
    """Raised when proposal frontmatter is missing or malformed."""


class InvalidProposalPathError(ValueError):
    """Raised when a slug or task_id would resolve outside vault/inbox/."""


class VaultProposer:
    def __init__(
        self,
        *,
        vault_root: Path,
        cluster_identity: tuple[str, str],
    ) -> None:
        """``cluster_identity`` is ``(name, email)`` used as the git author."""
        self._root = Path(vault_root).resolve()
        self._identity_name, self._identity_email = cluster_identity

    def propose(
        self,
        *,
        frontmatter: Mapping[str, Any],
        body: str,
        slug: str,
    ) -> Path:
        """Write a proposal note and commit it. Returns the absolute path."""
        self._validate_frontmatter(frontmatter)
        task_id = str(frontmatter["task_id"])
        self._validate_path_components(task_id=task_id, slug=slug)

        inbox_dir = self._root.joinpath(*_INBOX_SUBDIR, task_id)
        inbox_dir.mkdir(parents=True, exist_ok=True)

        path = inbox_dir / f"{slug}.md"
        # Belt and braces: the resolved path must remain inside vault/inbox/.
        if not path.resolve().is_relative_to(self._root.joinpath(*_INBOX_SUBDIR)):
            raise InvalidProposalPathError(f"resolved path {path} escapes vault/inbox/")

        path.write_text(_render(frontmatter, body), encoding="utf-8")

        rel = path.relative_to(self._root)
        self._git("add", str(rel))
        self._git_commit(
            message=f"vault_propose {task_id} {slug}",
        )
        return path

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _validate_frontmatter(frontmatter: Mapping[str, Any]) -> None:
        for field in REQUIRED_FRONTMATTER_FIELDS:
            if field not in frontmatter:
                raise InvalidFrontmatterError(f"missing required frontmatter field {field!r}")
        for ratio_field in ("confidence", "critic_score"):
            value = frontmatter[ratio_field]
            try:
                fv = float(value)
            except (TypeError, ValueError) as exc:
                raise InvalidFrontmatterError(
                    f"{ratio_field} must be a number, got {value!r}"
                ) from exc
            if not 0.0 <= fv <= 1.0:
                raise InvalidFrontmatterError(f"{ratio_field} must be in [0, 1], got {fv!r}")

    @staticmethod
    def _validate_path_components(*, task_id: str, slug: str) -> None:
        if not slug or not _SAFE_SLUG_RE.match(slug):
            raise InvalidProposalPathError(
                f"slug must match {_SAFE_SLUG_RE.pattern!r}, got {slug!r}"
            )
        if not _SAFE_TASK_ID_RE.match(task_id):
            raise InvalidProposalPathError(
                f"task_id must match {_SAFE_TASK_ID_RE.pattern!r}, got {task_id!r}"
            )

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self._root, check=True)

    def _git_commit(self, *, message: str) -> None:
        env = {
            "GIT_AUTHOR_NAME": self._identity_name,
            "GIT_AUTHOR_EMAIL": self._identity_email,
            "GIT_COMMITTER_NAME": self._identity_name,
            "GIT_COMMITTER_EMAIL": self._identity_email,
        }
        import os

        full_env = {**os.environ, **env}
        subprocess.run(
            ["git", "commit", "-q", "-m", message],
            cwd=self._root,
            check=True,
            env=full_env,
        )


def _render(frontmatter: Mapping[str, Any], body: str) -> str:
    lines = ["---"]
    for key in REQUIRED_FRONTMATTER_FIELDS:
        lines.append(f"{key}: {frontmatter[key]}")
    # Preserve any extra optional keys, sorted for stability
    for key in sorted(set(frontmatter) - set(REQUIRED_FRONTMATTER_FIELDS)):
        lines.append(f"{key}: {frontmatter[key]}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    if not body.endswith("\n"):
        lines.append("")
    return "\n".join(lines)
