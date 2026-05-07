"""AutonomyPolicy — graduates trust from manual to auto-within-quota."""

from __future__ import annotations

from enum import Enum

from turing.coordinator.training_approval.proposal import TrainingJobProposal
from turing.coordinator.training_approval.quota import QuotaTracker


class AutonomyMode(str, Enum):
    """How much trust the operator has granted the trainer.

    - ``MANUAL``: every proposal goes to Discord with empty fields.
    - ``MANUAL_WITH_DEFAULTS``: every proposal goes to Discord, but the UI
      pre-fills hyperparameters and dataset choices the trainer has been
      using successfully — one tap to confirm.
    - ``AUTO_WITHIN_QUOTA``: under-quota proposals are auto-approved without
      Discord interaction; over-quota proposals fall back to manual review.
    """

    MANUAL = "manual"
    MANUAL_WITH_DEFAULTS = "manual-with-defaults"
    AUTO_WITHIN_QUOTA = "auto-within-quota"


class PolicyDecision(str, Enum):
    REQUIRE_APPROVAL = "require_approval"
    REQUIRE_APPROVAL_WITH_DEFAULTS = "require_approval_with_defaults"
    AUTO_APPROVE = "auto_approve"


class AutonomyPolicy:
    def __init__(
        self,
        *,
        mode: AutonomyMode,
        quota_tracker: QuotaTracker,
    ) -> None:
        self._mode = mode
        self._quota = quota_tracker

    @property
    def mode(self) -> AutonomyMode:
        return self._mode

    def evaluate(self, proposal: TrainingJobProposal) -> PolicyDecision:
        if self._mode is AutonomyMode.MANUAL:
            return PolicyDecision.REQUIRE_APPROVAL
        if self._mode is AutonomyMode.MANUAL_WITH_DEFAULTS:
            return PolicyDecision.REQUIRE_APPROVAL_WITH_DEFAULTS
        # AUTO_WITHIN_QUOTA
        ok, _ = self._quota.check(
            proposal.specialty, cost_usd=proposal.estimated_cost_usd
        )
        if ok:
            return PolicyDecision.AUTO_APPROVE
        return PolicyDecision.REQUIRE_APPROVAL
