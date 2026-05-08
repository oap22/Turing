"""PromotionGate: eval delta check before staging."""

from __future__ import annotations

import pytest

from turing.coordinator.promotion import (
    EvalDeltaTooSmallError,
    PromotionGate,
    StageDecision,
)


def test_candidate_beats_baseline_by_threshold_returns_stage() -> None:
    gate = PromotionGate(min_delta=0.02)
    decision = gate.check_eval_delta(baseline_score=0.70, candidate_score=0.73)
    assert isinstance(decision, StageDecision)
    assert decision.delta == pytest.approx(0.03)


def test_candidate_below_threshold_raises() -> None:
    gate = PromotionGate(min_delta=0.02)
    with pytest.raises(EvalDeltaTooSmallError) as exc:
        gate.check_eval_delta(baseline_score=0.70, candidate_score=0.71)
    assert exc.value.delta == pytest.approx(0.01)
    assert exc.value.required == pytest.approx(0.02)


def test_candidate_worse_than_baseline_raises() -> None:
    gate = PromotionGate(min_delta=0.02)
    with pytest.raises(EvalDeltaTooSmallError):
        gate.check_eval_delta(baseline_score=0.70, candidate_score=0.65)


def test_exactly_at_threshold_passes() -> None:
    """Exactly 2% improvement is a pass — operators set the floor, not a
    just-above-floor sliver."""
    gate = PromotionGate(min_delta=0.02)
    decision = gate.check_eval_delta(baseline_score=0.70, candidate_score=0.72)
    assert isinstance(decision, StageDecision)
