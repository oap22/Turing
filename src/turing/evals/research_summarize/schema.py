"""Eval case schema for the `research-summarize` specialty.

A case is a single row in `cases/*.jsonl`. The harness loads cases, dispatches
the prompt+source_docs to a worker, and runs the named `scoring_fn` over the
returned summary. Each scoring_fn returns a float in [0, 1] plus a per-axis
breakdown for debugging.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["claim_preservation", "voice_match", "citation_correctness"]


class SourceDoc(BaseModel):
    id: str
    text: str
    url: str | None = None
    title: str | None = None


class VoiceFeatures(BaseModel):
    max_avg_sentence_len: float | None = None
    forbid_phrases: list[str] = Field(default_factory=list)
    # True = must contain first-person; False = must NOT; None = ignore.
    require_first_person: bool | None = None
    hedge_density_max: float | None = None
    max_paragraphs: int | None = None
    max_sentences_per_paragraph: int | None = None
    min_em_dash_count: int | None = None  # ASCII " -- ", operator's convention
    min_wikilink_count: int | None = None  # [[Note]] or [[src_id]] markers
    require_concrete_numbers: bool | None = None  # at least one digit-bearing token


class Expected(BaseModel):
    must_contain_claims: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    required_citations: dict[str, list[int]] = Field(default_factory=dict)
    voice_features: VoiceFeatures | None = None
    target_paragraph_count: int | None = None
    # Cosine threshold for embedding-based claim presence (overrides the
    # scorer's 0.75 default per case). Ignored when no embedder is wired in.
    claim_match_threshold: float | None = None


class EvalCase(BaseModel):
    id: str
    category: Category
    prompt: str
    source_docs: list[SourceDoc]
    expected: Expected
    scoring_fn: str
    notes: str | None = None
