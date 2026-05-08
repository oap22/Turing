"""PromotionGate — pre-stage adapter promotion checks.

The gate is the choke point between a freshly-trained adapter and the
fleet. It owns three independent checks (each its own method so callers
can audit which one fired):

1. ``check_eval_delta`` — candidate must beat baseline by ``min_delta``
   on the held-out set before anything else happens.
2. ``check_signature_and_sha`` — delegates to
   ``coordinator.adapters.AdapterRegistry.verify`` (signature + SHA256).
   Already-implemented in slice 20; we just call it.
3. ``record_live_eval`` + ``decide_rollout`` — the K-worker confirmation
   flow lives in ``rollout.RolloutCoordinator`` and uses these methods to
   detect regression mid-rollout.

We deliberately split eval-delta from signature/SHA into separate methods
so the audit trail says exactly which gate refused a candidate. A single
boolean return would lose that.
"""

from __future__ import annotations

from dataclasses import dataclass


class EvalDeltaTooSmallError(Exception):
    """Raised when a candidate adapter's eval improvement is below ``min_delta``."""

    def __init__(self, *, delta: float, required: float) -> None:
        super().__init__(
            f"eval delta {delta:.4f} < required {required:.4f}"
        )
        self.delta = delta
        self.required = required


@dataclass(frozen=True)
class StageDecision:
    """Returned by ``check_eval_delta`` when the candidate passes."""

    baseline_score: float
    candidate_score: float
    delta: float


class PromotionGate:
    def __init__(self, *, min_delta: float = 0.02) -> None:
        self._min_delta = min_delta

    def check_eval_delta(
        self,
        *,
        baseline_score: float,
        candidate_score: float,
    ) -> StageDecision:
        delta = candidate_score - baseline_score
        if delta < self._min_delta:
            raise EvalDeltaTooSmallError(delta=delta, required=self._min_delta)
        return StageDecision(
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            delta=delta,
        )
