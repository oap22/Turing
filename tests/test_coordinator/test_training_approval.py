"""Tests for the Discord training-job approval flow with quota graduation.

Covers issue #28's three deliverables:
- TrainingJobProposal model + Discord approval view (60s timeout pattern)
- AutonomyPolicy with three modes: manual, manual-with-defaults, auto-within-quota
- QuotaTracker enforcing per-specialty caps (jobs/week, $/week)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from turing.coordinator.training_approval.policy import (
    AutonomyMode,
    AutonomyPolicy,
    PolicyDecision,
)
from turing.coordinator.training_approval.proposal import TrainingJobProposal
from turing.coordinator.training_approval.quota import QuotaTracker, SpecialtyQuota

# ── proposal model ────────────────────────────────────────────────────


class TestTrainingJobProposal:
    def test_default_fields(self) -> None:
        p = TrainingJobProposal(
            specialty="research-summarize",
            base_model="qwen2.5-7b",
            method="sft",
            estimated_cost_usd=4.50,
            dataset_summary="120 turns from research workers",
        )
        assert p.specialty == "research-summarize"
        assert p.method == "sft"
        assert p.estimated_cost_usd == 4.50

    def test_method_must_be_sft_or_dpo(self) -> None:
        with pytest.raises(ValueError, match="method"):
            TrainingJobProposal(
                specialty="x",
                base_model="qwen2.5-7b",
                method="garbage",
                estimated_cost_usd=1.0,
                dataset_summary="",
            )


# ── quota tracker ─────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime(2026, 5, 7, 12, 0, tzinfo=UTC)


class TestQuotaTracker:
    def test_under_quota_when_no_history(self) -> None:
        tracker = QuotaTracker(now=_now)
        tracker.set_quota(
            "research-summarize",
            SpecialtyQuota(jobs_per_week=5, dollars_per_week=20.0),
        )
        ok, _ = tracker.check("research-summarize", cost_usd=5.0)
        assert ok is True

    def test_blocks_when_jobs_exhausted(self) -> None:
        tracker = QuotaTracker(now=_now)
        tracker.set_quota(
            "research-summarize",
            SpecialtyQuota(jobs_per_week=2, dollars_per_week=100.0),
        )
        for _ in range(2):
            tracker.record("research-summarize", cost_usd=1.0)
        ok, reason = tracker.check("research-summarize", cost_usd=1.0)
        assert ok is False
        assert "jobs" in reason.lower()

    def test_blocks_when_dollars_exceeded(self) -> None:
        tracker = QuotaTracker(now=_now)
        tracker.set_quota(
            "research-summarize",
            SpecialtyQuota(jobs_per_week=10, dollars_per_week=10.0),
        )
        tracker.record("research-summarize", cost_usd=8.0)
        ok, reason = tracker.check("research-summarize", cost_usd=5.0)
        assert ok is False
        assert "$" in reason or "dollar" in reason.lower()

    def test_per_specialty_isolation(self) -> None:
        tracker = QuotaTracker(now=_now)
        tracker.set_quota(
            "research-summarize",
            SpecialtyQuota(jobs_per_week=1, dollars_per_week=10.0),
        )
        tracker.set_quota(
            "code-debug",
            SpecialtyQuota(jobs_per_week=1, dollars_per_week=10.0),
        )
        tracker.record("research-summarize", cost_usd=1.0)
        ok, _ = tracker.check("code-debug", cost_usd=1.0)
        assert ok is True

    def test_old_records_age_out_of_week_window(self) -> None:
        anchor = _now()
        clock = {"now": anchor - timedelta(days=8)}

        def now_fn() -> datetime:
            return clock["now"]

        tracker = QuotaTracker(now=now_fn)
        tracker.set_quota(
            "research-summarize",
            SpecialtyQuota(jobs_per_week=1, dollars_per_week=10.0),
        )
        tracker.record("research-summarize", cost_usd=1.0)

        # Now advance time past the 7-day window
        clock["now"] = anchor
        ok, _ = tracker.check("research-summarize", cost_usd=1.0)
        assert ok is True

    def test_unknown_specialty_passes_check_with_no_quota(self) -> None:
        tracker = QuotaTracker(now=_now)
        ok, _ = tracker.check("never-heard-of-this", cost_usd=99.0)
        assert ok is True


# ── autonomy policy ───────────────────────────────────────────────────


class TestAutonomyPolicy:
    def _proposal(self, cost: float = 5.0) -> TrainingJobProposal:
        return TrainingJobProposal(
            specialty="research-summarize",
            base_model="qwen2.5-7b",
            method="sft",
            estimated_cost_usd=cost,
            dataset_summary="...",
        )

    def test_manual_mode_always_requires_approval(self) -> None:
        quota = QuotaTracker(now=_now)
        policy = AutonomyPolicy(mode=AutonomyMode.MANUAL, quota_tracker=quota)
        decision = policy.evaluate(self._proposal())
        assert decision is PolicyDecision.REQUIRE_APPROVAL

    def test_manual_with_defaults_still_requires_approval_but_prefills(
        self,
    ) -> None:
        quota = QuotaTracker(now=_now)
        policy = AutonomyPolicy(mode=AutonomyMode.MANUAL_WITH_DEFAULTS, quota_tracker=quota)
        decision = policy.evaluate(self._proposal())
        # Still goes to a Discord prompt — but the operator sees pre-filled
        # defaults so the click is one tap.
        assert decision is PolicyDecision.REQUIRE_APPROVAL_WITH_DEFAULTS

    def test_auto_within_quota_auto_approves_when_under(self) -> None:
        quota = QuotaTracker(now=_now)
        quota.set_quota(
            "research-summarize",
            SpecialtyQuota(jobs_per_week=10, dollars_per_week=100.0),
        )
        policy = AutonomyPolicy(mode=AutonomyMode.AUTO_WITHIN_QUOTA, quota_tracker=quota)
        decision = policy.evaluate(self._proposal(cost=5.0))
        assert decision is PolicyDecision.AUTO_APPROVE

    def test_auto_within_quota_falls_back_to_approval_when_over(self) -> None:
        quota = QuotaTracker(now=_now)
        quota.set_quota(
            "research-summarize",
            SpecialtyQuota(jobs_per_week=1, dollars_per_week=2.0),
        )
        quota.record("research-summarize", cost_usd=1.0)
        policy = AutonomyPolicy(mode=AutonomyMode.AUTO_WITHIN_QUOTA, quota_tracker=quota)
        # $5 > $2 cap remaining → can't auto-approve, escalate
        decision = policy.evaluate(self._proposal(cost=5.0))
        assert decision is PolicyDecision.REQUIRE_APPROVAL


# The Discord training-approval view (TrainingJobApprovalView) was deleted with
# ADR 0010; its approval affordance moves to the webui in a follow-on slice.
