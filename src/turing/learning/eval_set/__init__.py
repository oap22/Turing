"""Eval set authoring for research-summarize (#23)."""

from __future__ import annotations

from turing.learning.eval_set.case import EvalCase
from turing.learning.eval_set.judge import claim_recall
from turing.learning.eval_set.scorers import citation_exact_match, voice_cosine

__all__ = ["EvalCase", "citation_exact_match", "claim_recall", "voice_cosine"]
