"""Mechanical scorers: voice-independent and LLM-free."""

from __future__ import annotations


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
