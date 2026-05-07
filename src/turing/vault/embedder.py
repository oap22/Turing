"""Embedder protocol and a deterministic hash-derived implementation for tests.

Production uses an ONNX MiniLM model (`memory/embedding.py`); tests use the
hash-derived embedder so vault behaviour is exercised without loading
~80MB of weights and without flaky cosine values.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray


class Embedder(Protocol):
    def embed(self, text: str) -> NDArray[np.float32]: ...


class DeterministicHashEmbedder:
    """Token-bag embedder seeded from SHA256 hashes — stable across runs.

    Each whitespace-separated token contributes a deterministic unit vector
    derived from its hash; the document embedding is the L2-normalised sum.
    Cosine similarity then meaningfully reflects token overlap, which is all
    a deep-module test needs.
    """

    def __init__(self, *, dims: int = 64) -> None:
        if dims <= 0:
            raise ValueError("dims must be positive")
        self._dims = dims

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, text: str) -> NDArray[np.float32]:
        vec = np.zeros(self._dims, dtype=np.float32)
        for token in text.lower().split():
            vec += self._token_vector(token)
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            return vec
        return vec / norm

    def _token_vector(self, token: str) -> NDArray[np.float32]:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        # Stretch the 32-byte digest to fill `dims` floats in [-1, 1].
        repeats = (self._dims + len(digest) - 1) // len(digest)
        stretched = (digest * repeats)[: self._dims]
        arr = np.frombuffer(stretched, dtype=np.uint8).astype(np.float32)
        return (arr / 127.5) - 1.0
