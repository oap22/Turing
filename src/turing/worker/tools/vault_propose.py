"""``vault_propose`` — worker-side tool to add a proposal to the vault inbox.

Mirrors ``vault_query``'s shape: a structured-result dict so the worker
tool router can serialise it cleanly for audit logging and the planner
can reference the resulting path in downstream subtasks.
"""

from __future__ import annotations

from typing import Any, Mapping

from turing.vault.proposer import (
    InvalidFrontmatterError,
    InvalidProposalPathError,
    VaultProposer,
)


def vault_propose(
    *,
    proposer: VaultProposer,
    frontmatter: Mapping[str, Any],
    body: str,
    slug: str,
) -> dict[str, Any]:
    try:
        path = proposer.propose(frontmatter=frontmatter, body=body, slug=slug)
    except (InvalidFrontmatterError, InvalidProposalPathError) as exc:
        return {"error": str(exc), "task_id": frontmatter.get("task_id", "")}
    return {
        "path": str(path),
        "task_id": str(frontmatter["task_id"]),
        "slug": slug,
    }
