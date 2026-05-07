"""Tests for the worker-side vault_query tool wrapper."""

from __future__ import annotations

from turing.vault import DeterministicHashEmbedder, VaultIndex
from turing.worker.tools.vault_query import vault_query


def test_vault_query_returns_structured_hits_with_source_paths() -> None:
    index = VaultIndex(embedder=DeterministicHashEmbedder(dims=32))
    index.add_note(path="notes/python.md", text="python async tips")
    index.add_note(path="notes/cooking.md", text="how to roast vegetables")

    result = vault_query(index=index, query="python async", k=2)

    assert "hits" in result
    paths = [h["path"] for h in result["hits"]]
    assert "notes/python.md" in paths
    for hit in result["hits"]:
        assert "path" in hit and "snippet" in hit and "score" in hit


def test_vault_query_clamps_k_to_available() -> None:
    index = VaultIndex(embedder=DeterministicHashEmbedder(dims=16))
    index.add_note(path="a.md", text="alpha")

    result = vault_query(index=index, query="alpha", k=10)
    assert len(result["hits"]) == 1
