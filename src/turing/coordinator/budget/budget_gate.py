"""BudgetGate — pre-call interceptor for every cloud LLM call.

Estimates the call's cost from token counts × the model's price. Refuses
the call when the estimate would push today's spend over the daily cap and
signals the caller to fall back to a local model. The cap resets at local
midnight automatically because the SpendTracker only counts records dated
to today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from turing.coordinator.budget.spend_tracker import SpendTracker


class BudgetDecision(StrEnum):
    APPROVE = "approve"
    FALLBACK_LOCAL = "fallback_local"


@dataclass(frozen=True)
class BudgetCheckResult:
    action: BudgetDecision
    estimated_cents: int
    remaining_cents: int
    reason: str = ""


@dataclass(frozen=True)
class ModelPriceTable:
    """Per-model token prices in cents-per-1000-tokens.

    Two separate maps keep input vs. output prices honest — Anthropic's
    output tokens are several times more expensive than input.
    """

    cents_per_kilo_input: dict[str, int] = field(default_factory=dict)
    cents_per_kilo_output: dict[str, int] = field(default_factory=dict)

    def estimate(self, *, model: str, input_tokens: int, output_tokens: int) -> int:
        in_rate = self.cents_per_kilo_input.get(model, 0)
        out_rate = self.cents_per_kilo_output.get(model, 0)
        # Round up to nearest cent so we never under-estimate.
        cents = (input_tokens * in_rate + 500) // 1000
        cents += (output_tokens * out_rate + 500) // 1000
        return int(cents)


class BudgetGate:
    def __init__(
        self,
        *,
        daily_cap_cents: int,
        spend_tracker: SpendTracker,
        price_table: ModelPriceTable,
    ) -> None:
        self._cap = daily_cap_cents
        self._tracker = spend_tracker
        self._prices = price_table

    def check(
        self,
        *,
        task_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> BudgetCheckResult:
        estimate = self._prices.estimate(
            model=model, input_tokens=input_tokens, output_tokens=output_tokens
        )
        spent = self._tracker.spent_today()
        remaining = max(0, self._cap - spent)
        if spent + estimate > self._cap:
            return BudgetCheckResult(
                action=BudgetDecision.FALLBACK_LOCAL,
                estimated_cents=estimate,
                remaining_cents=remaining,
                reason=(
                    f"estimate ${estimate / 100:.2f} would push today's spend "
                    f"past ${self._cap / 100:.2f}"
                ),
            )
        return BudgetCheckResult(
            action=BudgetDecision.APPROVE,
            estimated_cents=estimate,
            remaining_cents=remaining,
        )

    def record_actual(
        self,
        *,
        task_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> int:
        cents = self._prices.estimate(
            model=model, input_tokens=input_tokens, output_tokens=output_tokens
        )
        self._tracker.record(task_id=task_id, cents=cents)
        return cents

    def remaining_cents(self) -> int:
        return max(0, self._cap - self._tracker.spent_today())


def format_remaining_budget(*, remaining_cents: int) -> str:
    """Render the Discord footer string. Cents → ``$D.CC`` always two-decimal."""
    dollars = remaining_cents // 100
    cents = remaining_cents % 100
    return f"budget remaining: ${dollars}.{cents:02d}"
