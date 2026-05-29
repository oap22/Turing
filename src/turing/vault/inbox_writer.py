"""InboxDraftWriter — writes a grounded research draft to ``vault/inbox/``.

ADR 0009 §2 "Night": a worker's draft lands at
``vault/inbox/<task_id>/<slug>.md`` with the existing frontmatter
(``source, task_id, specialty, confidence, critic_score``) **plus the fetched
source list** so the morning reviewer can check provenance.

This is the worker-side draft writer for the grounded-research path. Unlike
:class:`~turing.vault.proposer.VaultProposer` it does **not** git-commit — the
worker only writes the file into the inbox; promoting/committing the curated
result is the operator's morning concern. ``vault/inbox/**`` remains the only
writeable target: any ``task_id``/``slug`` that would resolve outside that
subtree is rejected before touching disk (shared safety rules with the
proposer).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from turing.vault.proposer import (
    _SAFE_SLUG_RE,
    _SAFE_TASK_ID_RE,
    REQUIRED_FRONTMATTER_FIELDS,
    InvalidFrontmatterError,
    InvalidProposalPathError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_INBOX_SUBDIR = ("vault", "inbox")


class InboxDraftWriter:
    """Writes grounded drafts under ``vault/inbox/<task_id>/`` (no git commit)."""

    def __init__(self, *, vault_root: Path) -> None:
        self._root = Path(vault_root).resolve()

    def write(
        self,
        *,
        frontmatter: Mapping[str, Any],
        sources: Sequence[Mapping[str, str]],
        body: str,
        slug: str,
    ) -> Path:
        """Write a draft with frontmatter + a ``sources`` list. Returns the path.

        ``sources`` is a sequence of ``{"id", "url", "title"}`` dicts (the
        fetched provenance). Raises :class:`InvalidFrontmatterError` /
        :class:`InvalidProposalPathError` on bad input — same contract as the
        proposer.
        """
        self._validate_frontmatter(frontmatter)
        task_id = str(frontmatter["task_id"])
        self._validate_path_components(task_id=task_id, slug=slug)

        inbox_root = self._root.joinpath(*_INBOX_SUBDIR)
        inbox_dir = inbox_root / task_id
        inbox_dir.mkdir(parents=True, exist_ok=True)

        path = inbox_dir / f"{slug}.md"
        if not path.resolve().is_relative_to(inbox_root):
            raise InvalidProposalPathError(f"resolved path {path} escapes vault/inbox/")

        path.write_text(_render(frontmatter, sources, body), encoding="utf-8")
        return path

    @staticmethod
    def _validate_frontmatter(frontmatter: Mapping[str, Any]) -> None:
        for field in REQUIRED_FRONTMATTER_FIELDS:
            if field not in frontmatter:
                raise InvalidFrontmatterError(f"missing required frontmatter field {field!r}")
        for ratio_field in ("confidence", "critic_score"):
            try:
                fv = float(frontmatter[ratio_field])
            except (TypeError, ValueError) as exc:
                raise InvalidFrontmatterError(
                    f"{ratio_field} must be a number, got {frontmatter[ratio_field]!r}"
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


def _render(
    frontmatter: Mapping[str, Any],
    sources: Sequence[Mapping[str, str]],
    body: str,
) -> str:
    lines = ["---"]
    for key in REQUIRED_FRONTMATTER_FIELDS:
        lines.append(f"{key}: {frontmatter[key]}")
    for key in sorted(set(frontmatter) - set(REQUIRED_FRONTMATTER_FIELDS) - {"sources"}):
        lines.append(f"{key}: {frontmatter[key]}")
    # The fetched source list as a YAML block — provenance for morning review.
    lines.append("sources:")
    for src in sources:
        lines.append(f"  - id: {src.get('id', '')}")
        lines.append(f"    url: {src.get('url', '')}")
        lines.append(f"    title: {_yaml_scalar(src.get('title', ''))}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    if not body.endswith("\n"):
        lines.append("")
    return "\n".join(lines)


def _yaml_scalar(value: str) -> str:
    """Quote a title if it contains YAML-significant characters."""
    if (value and any(ch in value for ch in ":#\n")) or value != value.strip():
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value
