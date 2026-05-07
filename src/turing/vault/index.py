"""VaultIndex — incremental vector index of the operator's Obsidian vault.

Workers ground outputs by calling `vault_query(query, k)`. Updates land
incrementally on every commit (a filesystem watcher will call `add_note` /
`remove_note` per changed file), so neither must touch the rest of the
index — O(1) per update, never a full resweep.

`add_note` overwrites any prior entry for the same path; `remove_note` is
idempotent. `query` returns top-k hits ordered by cosine similarity desc,
each carrying the source path and a snippet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from turing.vault.embedder import Embedder


_SNIPPET_CHARS = 280


@dataclass(frozen=True)
class VaultHit:
    path: str
    snippet: str
    score: float


@dataclass
class _Entry:
    text: str
    embedding: NDArray[np.float32]


class VaultIndex:
    def __init__(self, *, embedder: Embedder) -> None:
        self._embedder = embedder
        self._entries: dict[str, _Entry] = {}

    def add_note(self, *, path: str, text: str) -> None:
        self._entries[path] = _Entry(text=text, embedding=self._embedder.embed(text))

    def remove_note(self, *, path: str) -> None:
        self._entries.pop(path, None)

    def snapshot(self) -> dict[str, str]:
        return {path: entry.text for path, entry in self._entries.items()}

    def query(self, query: str, *, k: int) -> list[VaultHit]:
        if not self._entries:
            return []
        q = self._embedder.embed(query)
        scored = [
            (path, float(np.dot(q, entry.embedding)), entry.text)
            for path, entry in self._entries.items()
        ]
        scored.sort(key=lambda row: row[1], reverse=True)
        return [
            VaultHit(path=path, snippet=_snippet_for(query, text), score=score)
            for path, score, text in scored[:k]
        ]


def _snippet_for(query: str, text: str) -> str:
    """Return a window of `text` around the first query-token match.

    Falls back to the leading characters when nothing matches.
    """
    needle = next((tok for tok in query.lower().split() if tok in text.lower()), None)
    if needle is None:
        return text[:_SNIPPET_CHARS]
    idx = text.lower().find(needle)
    start = max(0, idx - _SNIPPET_CHARS // 2)
    return text[start : start + _SNIPPET_CHARS]
