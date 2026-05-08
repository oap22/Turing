"""Adapter promotion gate with K-worker live re-eval and halt-on-regression."""

from __future__ import annotations

from turing.coordinator.promotion.hard_examples import (
    HardExample,
    archive_failed_training,
)
from turing.coordinator.promotion.promotion_gate import (
    EvalDeltaTooSmallError,
    PromotionGate,
    StageDecision,
)
from turing.coordinator.promotion.rollout import (
    LiveEvalReport,
    RegressionHaltedError,
    RolloutCoordinator,
    RolloutState,
)

__all__ = [
    "EvalDeltaTooSmallError",
    "HardExample",
    "LiveEvalReport",
    "PromotionGate",
    "RegressionHaltedError",
    "RolloutCoordinator",
    "RolloutState",
    "StageDecision",
    "archive_failed_training",
]
