"""Mechanical scorers: voice-independent and LLM-free."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence


def citation_exact_match(output: str, expected_citations: list[str]) -> float:
    """Fraction of ``expected_citations`` that appear verbatim in ``output``.

    Citations are matched as exact substrings — a corrupted citation
    (e.g. ``[12]`` when ``[1]`` was expected) does not silently pass
    because the verbatim form of ``[1]`` is not present. We avoid
    fuzzy matching deliberately: the gate's job is to catch
    paraphrasers that mangle reference numbers.
    """
    if not expected_citations:
        return 1.0
    hits = sum(1 for cite in expected_citations if cite in output)
    return hits / len(expected_citations)


Embedder = Callable[[str], Sequence[float]]


def voice_cosine(
    output: str,
    voice_ref_embedding: Sequence[float],
    *,
    embed: Embedder,
) -> float:
    """Cosine similarity in [0, 1] between ``output`` and ``voice_ref_embedding``.

    Negative cosine values are clamped to 0.0 — the voice-match axis is a
    "how close" signal, not an antonym detector. The embedder is injected so
    tests can use a deterministic stand-in and production can plug in the
    real ONNX model.
    """
    out_vec = list(embed(output))
    ref_vec = list(voice_ref_embedding)
    if len(out_vec) != len(ref_vec):
        return 0.0
    out_norm = math.sqrt(sum(x * x for x in out_vec))
    ref_norm = math.sqrt(sum(x * x for x in ref_vec))
    if out_norm == 0.0 or ref_norm == 0.0:
        return 0.0
    # Indexed iteration: lengths are pre-checked above, and this avoids
    # the zip(strict=...) Python 3.10+ requirement.
    dot = sum(out_vec[i] * ref_vec[i] for i in range(len(out_vec)))
    return max(0.0, dot / (out_norm * ref_norm))
