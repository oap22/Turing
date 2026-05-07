"""Tests for VaultIndex — vector index of the operator's Obsidian vault.

Workers ground outputs in the vault by calling `vault_query(query, k)`.
Updates land incrementally on every commit (filesystem watcher), so add /
remove must be O(1) on the index — never a full resweep.

Tests use a deterministic hash-derived embedder so behaviour is stable
without touching the real ONNX model.
"""

from __future__ import annotations

import pytest

from turing.vault.embedder import DeterministicHashEmbedder
from turing.vault.index import VaultIndex


@pytest.fixture()
def index() -> VaultIndex:
    return VaultIndex(embedder=DeterministicHashEmbedder(dims=32))


def test_add_then_query_returns_the_note(index: VaultIndex) -> None:
    index.add_note(path="notes/python.md", text="python async tips and tricks")

    hits = index.query("python async", k=1)

    assert [h.path for h in hits] == ["notes/python.md"]


def test_query_orders_results_by_similarity_descending(index: VaultIndex) -> None:
    index.add_note(path="notes/python.md", text="python async tips")
    index.add_note(path="notes/cooking.md", text="how to roast vegetables")
    index.add_note(path="notes/asyncio.md", text="python async tips")

    hits = index.query("python async tips", k=3)

    assert hits[0].score >= hits[1].score >= hits[2].score


def test_query_respects_k(index: VaultIndex) -> None:
    for i in range(5):
        index.add_note(path=f"notes/{i}.md", text=f"note {i}")

    hits = index.query("note", k=2)
    assert len(hits) == 2


def test_add_note_overwrites_existing_path(index: VaultIndex) -> None:
    index.add_note(path="notes/python.md", text="old text")
    index.add_note(path="notes/python.md", text="new text")

    snapshot = index.snapshot()
    assert snapshot["notes/python.md"] == "new text"
    assert len(snapshot) == 1


def test_remove_note_drops_from_query_results(index: VaultIndex) -> None:
    index.add_note(path="notes/python.md", text="python")
    index.add_note(path="notes/cooking.md", text="cooking")
    index.remove_note(path="notes/python.md")

    hits = index.query("python", k=5)
    assert "notes/python.md" not in {h.path for h in hits}


def test_remove_unknown_path_is_idempotent_no_op(index: VaultIndex) -> None:
    index.remove_note(path="notes/missing.md")  # does not raise


def test_query_returns_source_path_and_snippet(index: VaultIndex) -> None:
    index.add_note(
        path="notes/python.md",
        text="python async tips and tricks for high-throughput pipelines",
    )

    hits = index.query("python async", k=1)

    assert hits[0].path == "notes/python.md"
    assert "python async" in hits[0].snippet


def test_empty_index_returns_no_hits(index: VaultIndex) -> None:
    hits = index.query("anything", k=5)
    assert hits == []


def test_deterministic_embedder_is_stable() -> None:
    embedder = DeterministicHashEmbedder(dims=16)

    a = embedder.embed("python async tips")
    b = embedder.embed("python async tips")

    assert (a == b).all()
    assert a.shape == (16,)
