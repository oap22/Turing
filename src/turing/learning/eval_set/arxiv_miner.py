"""Mine candidate EvalCase rows from arxiv papers.

Sentence-extraction is mechanical: the abstract is split on sentence
boundaries and each sentence becomes an ``expected_claim``. Citation
markers are extracted by regex from the paper body. We deliberately never
ask an LLM to write a claim — that would be circular and would poison the
gate. The operator's job is to curate the candidates this miner produces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ArxivPaper:
    arxiv_id: str
    title: str
    abstract: str
    body: str


# Numeric or alphanumeric-with-digits citation markers. Excludes ``[foo]``-style
# free-text markers that aren't real reference numbers.
_CITATION_RE = re.compile(r"\[\d+[a-z]*\]")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def mine_eval_case(paper: ArxivPaper) -> dict[str, Any]:
    """Produce a candidate EvalCase dict from an arxiv paper fixture."""
    expected_claims = _split_sentences(paper.abstract)
    expected_citations = _extract_citations(paper.body)
    return {
        "id": f"arxiv-{paper.arxiv_id}",
        "input": f"summarise the paper: {paper.title}",
        "source_refs": [f"arxiv:{paper.arxiv_id}"],
        "expected_claims": expected_claims,
        "expected_citations": expected_citations,
        "voice_ref_id": None,
        "axes": ["claim_preservation", "citation_correctness"],
    }


def _split_sentences(text: str) -> list[str]:
    """Split on sentence boundaries; trim whitespace; drop empties."""
    parts = _SENTENCE_BOUNDARY.split(text.strip())
    return [s.strip() for s in parts if s.strip()]


def _extract_citations(text: str) -> list[str]:
    """De-duplicate citations while preserving first-seen order."""
    seen: dict[str, None] = {}
    for match in _CITATION_RE.finditer(text):
        seen.setdefault(match.group(0), None)
    return list(seen.keys())
