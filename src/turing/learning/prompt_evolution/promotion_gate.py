"""PromotionGate — decides whether a candidate prompt version replaces baseline.

Policy:
- ``min_sample_size``: minimum episode count on each side. Below this, return
  HOLD_INSUFFICIENT_DATA — no decision until both arms have run enough.
- ``win_rate_threshold``: fraction of candidate-vs-baseline pair-ups the
  candidate must win, by mean critic score.
- ``holdout_min_size``: minimum episode count in the baseline arm specifically;
  prevents promotion when baseline traffic is too thin to be a real holdout.

Real Phase A nightlies layer this on top of a population-level statistical
test; this slice keeps the gate transparent and unit-testable.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from turing.coordinator.lifecycle.episode_store import Episode


class PromotionDecision(str, Enum):
    PROMOTE = "promote"
    REJECT = "reject"
    HOLD_INSUFFICIENT_DATA = "hold_insufficient_data"


@dataclass(frozen=True)
class PromotionGate:
    min_sample_size: int
    win_rate_threshold: float
    holdout_min_size: int

    def evaluate(
        self,
        *,
        candidate: Sequence[Episode],
        baseline: Sequence[Episode],
    ) -> PromotionDecision:
        if (
            len(candidate) < self.min_sample_size
            or len(baseline) < self.holdout_min_size
        ):
            return PromotionDecision.HOLD_INSUFFICIENT_DATA

        cand_mean = statistics.fmean(e.critic_score for e in candidate)
        base_mean = statistics.fmean(e.critic_score for e in baseline)
        if cand_mean <= base_mean:
            return PromotionDecision.REJECT

        # Pairwise win rate: fraction of candidate scores strictly beating
        # the baseline mean. Robust against a tiny number of high outliers
        # in the baseline.
        wins = sum(1 for e in candidate if e.critic_score > base_mean)
        win_rate = wins / len(candidate)
        if win_rate >= self.win_rate_threshold:
            return PromotionDecision.PROMOTE
        return PromotionDecision.REJECT
