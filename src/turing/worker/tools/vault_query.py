"""`vault_query` — worker-side low-risk tool grounding outputs in the vault.

The function shape matches the structured-result contract used by the rest
of the worker tool router: a JSON-ish dict so it serialises cleanly for
audit logging and downstream prompt injection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from turing.vault.index import VaultIndex


def vault_query(*, index: VaultIndex, query: str, k: int) -> dict[str, Any]:
    hits = index.query(query, k=k)
    return {
        "query": query,
        "hits": [{"path": hit.path, "snippet": hit.snippet, "score": hit.score} for hit in hits],
    }
