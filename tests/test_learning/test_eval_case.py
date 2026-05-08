"""Tracer bullet for the EvalCase schema."""

from __future__ import annotations

from turing.learning.eval_set import EvalCase


def test_eval_case_round_trips_minimum_fields() -> None:
    raw = {
        "id": "rs-001",
        "input": "summarise this paper",
        "source_refs": ["arxiv:2401.00001"],
        "expected_claims": ["LLMs hallucinate at long contexts"],
        "expected_citations": ["[1]"],
        "voice_ref_id": None,
        "axes": ["claim_preservation", "citation_correctness"],
    }
    case = EvalCase.model_validate(raw)
    assert case.id == "rs-001"
    assert case.voice_ref_id is None
    assert case.expected_claims == ["LLMs hallucinate at long contexts"]
    # Round-trip preserves shape
    assert case.model_dump() == raw
