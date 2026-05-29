"""Collapse-aware eval gate (ADR 0009 §4).

The ADR's eval guardrail is explicit: **eval-gate on held-out *real* data +
diversity/tail metrics, not mean accuracy alone.** Model collapse erodes output
diversity and the distribution's tails *before* mean accuracy visibly drops, so
a mean-only gate (the ADR 0007 ≥2pp held-out improvement check) misses early
collapse (Nature 2024; arXiv:2502.08512, 2511.01490).

This gate extends — does not replace — the mean-delta check:

1. **Held-out real improvement** — candidate must beat baseline by ``min_delta``
   on the held-out *real* (not synthetic) eval set. (Same spirit as
   :class:`~turing.coordinator.promotion.promotion_gate.PromotionGate`.)
2. **Diversity floor** — the candidate's output diversity (distinct-n /
   type-token ratio over its eval outputs) must not fall more than
   ``max_diversity_drop`` below the baseline's. A candidate that scores well but
   collapses to a few templated phrasings is refused.
3. **Tail coverage floor** — the fraction of held-out cases the candidate gets
   *non-zero* credit on (the distribution tail, not just the easy mode) must not
   fall more than ``max_tail_drop`` below baseline.

All three must pass to STAGE. Each failure raises a distinct error so the audit
trail records which signal caught the regression.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_WORD_RE = re.compile(r"[a-z0-9]+")


class CollapseGateError(Exception):
    """Base class for collapse-gate refusals."""


class MeanDeltaTooSmallError(CollapseGateError):
    def __init__(self, *, delta: float, required: float) -> None:
        super().__init__(f"held-out mean delta {delta:.4f} < required {required:.4f}")
        self.delta = delta
        self.required = required


class DiversityCollapseError(CollapseGateError):
    def __init__(self, *, baseline: float, candidate: float, max_drop: float) -> None:
        super().__init__(
            f"diversity dropped {baseline - candidate:.4f} "
            f"(baseline {baseline:.4f} → candidate {candidate:.4f}) > max {max_drop:.4f}"
        )
        self.baseline = baseline
        self.candidate = candidate
        self.max_drop = max_drop


class TailCollapseError(CollapseGateError):
    def __init__(self, *, baseline: float, candidate: float, max_drop: float) -> None:
        super().__init__(
            f"tail coverage dropped {baseline - candidate:.4f} "
            f"(baseline {baseline:.4f} → candidate {candidate:.4f}) > max {max_drop:.4f}"
        )
        self.baseline = baseline
        self.candidate = candidate
        self.max_drop = max_drop


@dataclass(frozen=True)
class EvalRun:
    """Per-case scores + outputs from running one model over the held-out set.

    ``scores`` and ``outputs`` are aligned (same case order, same length).
    ``scores`` are per-case in [0, 1]; ``outputs`` are the generated texts used
    to measure diversity.
    """

    scores: tuple[float, ...]
    outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.scores) != len(self.outputs):
            raise ValueError("scores and outputs must be the same length")
        if not self.scores:
            raise ValueError("eval run must have at least one case")

    @property
    def mean_score(self) -> float:
        return sum(self.scores) / len(self.scores)

    @property
    def tail_coverage(self) -> float:
        """Fraction of cases scoring > 0 — coverage of the distribution tail."""
        return sum(1 for s in self.scores if s > 0.0) / len(self.scores)

    @property
    def diversity(self) -> float:
        """Distinct-token ratio over all outputs (a type-token diversity proxy)."""
        return distinct_token_ratio(self.outputs)


@dataclass(frozen=True)
class CollapseGateDecision:
    """Returned when a candidate clears every collapse check."""

    mean_delta: float
    baseline_diversity: float
    candidate_diversity: float
    baseline_tail: float
    candidate_tail: float


def distinct_token_ratio(outputs: Sequence[str]) -> float:
    """Ratio of distinct word tokens to total word tokens across ``outputs``.

    1.0 = every token unique (maximally diverse); approaches 0 as outputs
    collapse to repeated phrasings. Empty input → 0.0.
    """
    total = 0
    distinct: set[str] = set()
    for text in outputs:
        toks = _WORD_RE.findall(text.lower())
        total += len(toks)
        distinct.update(toks)
    if total == 0:
        return 0.0
    return len(distinct) / total


class CollapseAwareEvalGate:
    """Mean-delta + diversity-floor + tail-floor promotion gate."""

    def __init__(
        self,
        *,
        min_delta: float = 0.02,
        max_diversity_drop: float = 0.10,
        max_tail_drop: float = 0.05,
    ) -> None:
        self._min_delta = min_delta
        self._max_diversity_drop = max_diversity_drop
        self._max_tail_drop = max_tail_drop

    def check(self, *, baseline: EvalRun, candidate: EvalRun) -> CollapseGateDecision:
        """Raise the relevant ``CollapseGateError`` unless all three checks pass."""
        delta = candidate.mean_score - baseline.mean_score
        if delta < self._min_delta:
            raise MeanDeltaTooSmallError(delta=delta, required=self._min_delta)

        if baseline.diversity - candidate.diversity > self._max_diversity_drop:
            raise DiversityCollapseError(
                baseline=baseline.diversity,
                candidate=candidate.diversity,
                max_drop=self._max_diversity_drop,
            )

        if baseline.tail_coverage - candidate.tail_coverage > self._max_tail_drop:
            raise TailCollapseError(
                baseline=baseline.tail_coverage,
                candidate=candidate.tail_coverage,
                max_drop=self._max_tail_drop,
            )

        return CollapseGateDecision(
            mean_delta=delta,
            baseline_diversity=baseline.diversity,
            candidate_diversity=candidate.diversity,
            baseline_tail=baseline.tail_coverage,
            candidate_tail=candidate.tail_coverage,
        )
