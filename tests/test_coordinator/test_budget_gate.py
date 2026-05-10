"""Tests for the cloud-LLM budget gate (#14).

Three deliverables:

1. ``BudgetGate`` deep module: pre-call estimate, refusal at cap, midnight reset,
   fallback-to-local signal.
2. Hard daily cap configurable via ``TURING_BUDGET_DAILY_CAP_CENTS`` (default
   $10/day = 1000 cents).
3. ``Episode.cents_spent`` field so ``SELECT task_id, cents_spent`` is reportable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from turing.coordinator.budget import (
    BudgetDecision,
    BudgetGate,
    ModelPriceTable,
    SpendTracker,
)
from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState

# ── ModelPriceTable / estimate ────────────────────────────────────────


class TestEstimate:
    def test_estimate_sums_input_and_output_tokens(self) -> None:
        prices = ModelPriceTable(
            cents_per_kilo_input={"claude-sonnet-4": 100},  # $1 / 1K input
            cents_per_kilo_output={"claude-sonnet-4": 500},  # $5 / 1K output
        )
        cents = prices.estimate(model="claude-sonnet-4", input_tokens=2000, output_tokens=1000)
        # 2K input * $1/K + 1K output * $5/K = $2 + $5 = $7 = 700 cents
        assert cents == 700

    def test_unknown_model_falls_back_to_zero(self) -> None:
        prices = ModelPriceTable(cents_per_kilo_input={}, cents_per_kilo_output={})
        cents = prices.estimate(model="unknown-model", input_tokens=1000, output_tokens=1000)
        assert cents == 0


# ── SpendTracker ──────────────────────────────────────────────────────


def _now(year: int = 2026, month: int = 5, day: int = 7, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


class TestSpendTracker:
    def test_record_increments_today(self) -> None:
        clock = {"now": _now()}
        tracker = SpendTracker(now=lambda: clock["now"])
        tracker.record(task_id="t-1", cents=200)
        tracker.record(task_id="t-2", cents=300)
        assert tracker.spent_today() == 500

    def test_resets_at_local_midnight_utc_for_test(self) -> None:
        clock = {"now": _now(hour=23)}
        tracker = SpendTracker(now=lambda: clock["now"])
        tracker.record(task_id="t-1", cents=200)
        # Roll to next day
        clock["now"] = clock["now"] + timedelta(hours=2)
        assert tracker.spent_today() == 0

    def test_per_task_attribution(self) -> None:
        tracker = SpendTracker(now=_now)
        tracker.record(task_id="t-1", cents=100)
        tracker.record(task_id="t-1", cents=50)
        tracker.record(task_id="t-2", cents=200)
        assert tracker.spent_for_task("t-1") == 150
        assert tracker.spent_for_task("t-2") == 200


# ── BudgetGate ────────────────────────────────────────────────────────


def _gate(
    *,
    cap_cents: int = 1000,
    spent_cents: int = 0,
    prices: ModelPriceTable | None = None,
) -> tuple[BudgetGate, SpendTracker]:
    tracker = SpendTracker(now=_now)
    if spent_cents:
        tracker.record(task_id="seed", cents=spent_cents)
    prices = prices or ModelPriceTable(
        cents_per_kilo_input={"claude-sonnet-4": 100},
        cents_per_kilo_output={"claude-sonnet-4": 500},
    )
    gate = BudgetGate(
        daily_cap_cents=cap_cents,
        spend_tracker=tracker,
        price_table=prices,
    )
    return gate, tracker


class TestBudgetGate:
    def test_approves_call_under_cap(self) -> None:
        gate, _ = _gate(cap_cents=1000, spent_cents=0)
        decision = gate.check(
            task_id="t-1",
            model="claude-sonnet-4",
            input_tokens=1000,
            output_tokens=500,
        )
        assert decision.action is BudgetDecision.APPROVE
        # 1K input * 100/K + 0.5K output * 500/K = 100 + 250 = 350 cents estimated
        assert decision.estimated_cents == 350

    def test_refuses_when_estimate_would_exceed_cap(self) -> None:
        gate, _ = _gate(cap_cents=1000, spent_cents=900)
        decision = gate.check(
            task_id="t-1",
            model="claude-sonnet-4",
            input_tokens=1000,
            output_tokens=500,
        )
        # Already $9 spent, 350 cents projected → $12.50 > $10 cap
        assert decision.action is BudgetDecision.FALLBACK_LOCAL
        assert decision.reason  # explanation present

    def test_records_actual_spend_after_call(self) -> None:
        gate, tracker = _gate()
        gate.record_actual(
            task_id="t-1",
            model="claude-sonnet-4",
            input_tokens=1000,
            output_tokens=500,
        )
        assert tracker.spent_today() == 350
        assert tracker.spent_for_task("t-1") == 350

    def test_remaining_cents_reflects_spend(self) -> None:
        gate, _ = _gate(cap_cents=1000, spent_cents=200)
        assert gate.remaining_cents() == 800

    def test_remaining_never_negative(self) -> None:
        gate, _ = _gate(cap_cents=1000, spent_cents=2000)
        assert gate.remaining_cents() == 0

    def test_midnight_reset_replenishes_budget(self) -> None:
        clock = {"now": _now(hour=23)}
        tracker = SpendTracker(now=lambda: clock["now"])
        tracker.record(task_id="t-seed", cents=900)
        prices = ModelPriceTable(
            cents_per_kilo_input={"claude-sonnet-4": 100},
            cents_per_kilo_output={"claude-sonnet-4": 500},
        )
        gate = BudgetGate(daily_cap_cents=1000, spend_tracker=tracker, price_table=prices)
        # Pre-rollover: a $4 call would exceed
        d1 = gate.check(
            task_id="t-1",
            model="claude-sonnet-4",
            input_tokens=2000,
            output_tokens=400,
        )
        assert d1.action is BudgetDecision.FALLBACK_LOCAL

        # Roll over midnight
        clock["now"] = clock["now"] + timedelta(hours=2)
        d2 = gate.check(
            task_id="t-2",
            model="claude-sonnet-4",
            input_tokens=2000,
            output_tokens=400,
        )
        assert d2.action is BudgetDecision.APPROVE


# ── Episode.cents_spent ───────────────────────────────────────────────


class TestEpisodeCentsSpent:
    def test_episode_records_cents_spent(self) -> None:
        ep = Episode(
            task_id="t-1",
            subtask_id="t-1.s-0",
            worker_id="w",
            specialty="x",
            model_version="m",
            adapter_version="a",
            input_text="i",
            trajectory=("a",),
            output_text="o",
            success=True,
            latency_ms=1,
            tokens_used=10,
            outcome=SubtaskState.COMPLETED,
            critic_score=0.5,
            recorded_at_ms=0,
            cents_spent=42,
        )
        assert ep.cents_spent == 42

    def test_episode_cents_spent_default_zero(self) -> None:
        ep = Episode(
            task_id="t-1",
            subtask_id="t-1.s-0",
            worker_id="w",
            specialty="x",
            model_version="m",
            adapter_version="a",
            input_text="i",
            trajectory=("a",),
            output_text="o",
            success=True,
            latency_ms=1,
            tokens_used=10,
            outcome=SubtaskState.COMPLETED,
            critic_score=0.5,
            recorded_at_ms=0,
        )
        assert ep.cents_spent == 0


# ── Discord footer ────────────────────────────────────────────────────


class TestBudgetFooter:
    def test_footer_renders_dollars_remaining(self) -> None:
        from turing.coordinator.budget import format_remaining_budget

        assert format_remaining_budget(remaining_cents=534) == ("budget remaining: $5.34")

    def test_footer_zero_remaining(self) -> None:
        from turing.coordinator.budget import format_remaining_budget

        assert format_remaining_budget(remaining_cents=0) == ("budget remaining: $0.00")
