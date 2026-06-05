"""Vault index, embedder, proposer, and tool wrappers.

Re-exports are loaded lazily via ``__getattr__`` so ``import turing.vault.X``
doesn't pull in numpy unless the caller actually touches the index path.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CommitDiff",
    "DeterministicHashEmbedder",
    "Embedder",
    "InvalidFrontmatterError",
    "InvalidProposalPathError",
    "VaultCommitter",
    "VaultHit",
    "VaultIndex",
    "VaultProposer",
    "VaultWatcher",
    "promotion_commit_message",
]


def __getattr__(name: str) -> Any:
    if name in ("DeterministicHashEmbedder", "Embedder"):
        from turing.vault import embedder

        return getattr(embedder, name)
    if name in ("VaultHit", "VaultIndex"):
        from turing.vault import index

        return getattr(index, name)
    if name in ("CommitDiff", "VaultWatcher"):
        from turing.vault import watcher

        return getattr(watcher, name)
    if name in ("VaultCommitter", "promotion_commit_message"):
        from turing.vault import committer

        return getattr(committer, name)
    if name in (
        "InvalidFrontmatterError",
        "InvalidProposalPathError",
        "VaultProposer",
    ):
        from turing.vault import proposer

        return getattr(proposer, name)
    raise AttributeError(f"module 'turing.vault' has no attribute {name!r}")
