"""Tests for the arxiv eval-case miner.

The miner takes arxiv abstracts (a fixture stand-in for the real arxiv API)
and produces candidate ``EvalCase`` rows: ``expected_claims`` extracted
from the abstract sentences, ``expected_citations`` from the bibliography
markers it sees in the paper text. ``voice_ref_id`` is left null — the
operator decides whether to anchor a case to their voice corpus later.

The miner never asks an LLM to *write* an expected_*. Sentence
extraction is mechanical; that's the whole point.
"""

from __future__ import annotations

from turing.learning.eval_set.arxiv_miner import (
    ArxivPaper,
    mine_eval_case,
)


def _paper(
    *,
    arxiv_id: str = "2401.00001",
    title: str = "On the limits of long-context retrieval",
    abstract: str = (
        "Large language models hallucinate at long contexts. "
        "We show recall drops sharply past 32k tokens. "
        "Our benchmark uses 200 papers."
    ),
    body: str = "See [1] for prior work; [2] for our setup.",
) -> ArxivPaper:
    return ArxivPaper(
        arxiv_id=arxiv_id,
        title=title,
        abstract=abstract,
        body=body,
    )


def test_each_abstract_sentence_becomes_an_expected_claim() -> None:
    case = mine_eval_case(_paper())
    # Abstract has three sentences; each becomes an expected claim.
    assert len(case["expected_claims"]) == 3
    assert "Large language models hallucinate at long contexts." in case["expected_claims"]


def test_citations_extracted_from_body() -> None:
    case = mine_eval_case(_paper(body="See [1] and also [2]; not [foo]."))
    assert "[1]" in case["expected_citations"]
    assert "[2]" in case["expected_citations"]
    assert "[foo]" not in case["expected_citations"]


def test_voice_ref_id_is_null() -> None:
    """Mined cases ship without a voice anchor — operator decides."""
    case = mine_eval_case(_paper())
    assert case["voice_ref_id"] is None


def test_id_includes_arxiv_id() -> None:
    case = mine_eval_case(_paper(arxiv_id="2401.99999"))
    assert "2401.99999" in case["id"]


def test_input_field_uses_paper_title() -> None:
    case = mine_eval_case(_paper(title="On the limits of long-context retrieval"))
    assert "long-context retrieval" in case["input"]


def test_axes_default_to_voice_independent() -> None:
    """Mined cases don't have a voice anchor, so the axes default to the
    two voice-independent ones."""
    case = mine_eval_case(_paper())
    assert "claim_preservation" in case["axes"]
    assert "citation_correctness" in case["axes"]
    assert "voice_match" not in case["axes"]


def test_deterministic_for_same_input() -> None:
    """Given the same fixture, the miner produces byte-identical output —
    that's the property tests need to pin behavior on."""
    a = mine_eval_case(_paper())
    b = mine_eval_case(_paper())
    assert a == b


def test_output_validates_against_eval_case_schema() -> None:
    """The miner's output is a candidate EvalCase row; round-trip it
    through the pydantic schema to catch any drift."""
    from turing.learning.eval_set import EvalCase

    raw = mine_eval_case(_paper())
    EvalCase.model_validate(raw)
