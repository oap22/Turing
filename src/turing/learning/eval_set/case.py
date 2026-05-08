"""Pydantic schema for one eval row.

The voice-match axis stays a stub field (``voice_ref_id``) — it's filled by
pairwise preference collection through the Discord thumbs flow once
preferences accumulate. We never let an LLM synthesise an ``expected_*``
field; all expecteds are mined from real sources or hand-authored.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class EvalCase(BaseModel):
    """One row in ``evals/research-summarize/*.jsonl``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    input: str
    source_refs: list[str]
    expected_claims: list[str]
    expected_citations: list[str]
    voice_ref_id: str | None
    axes: list[str]
