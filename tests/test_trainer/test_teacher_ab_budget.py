"""Tests for the budget-gated teacher A/B (issue #270, ADR 0009 §4).

The A/B still picks by student transfer (TeacherABHarness), but each teacher's
cloud polish pass must pass the $10/day BudgetGate first; unaffordable arms are
skipped and the actual spend is recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from turing.coordinator.budget.budget_gate import BudgetGate, ModelPriceTable
from turing.coordinator.budget.spend_tracker import SpendTracker
from turing.coordinator.flywheel.morning_curation import SFTCandidate
from turing.learning.eval_set.collapse_gate import EvalRun
from turing.learning.trainer import (
    AllTeachersOverBudgetError,
    BudgetedTeacher,
    BudgetedTeacherAB,
    TeacherCandidate,
)

CURATED = [SFTCandidate(question="q", reasoning="r", answer="a", specialty="ai-ml-generalist")]


def _prices() -> ModelPriceTable:
    # cents per 1000 tokens. "huge" is dramatically pricier than "mid"/"small".
    return ModelPriceTable(
        cents_per_kilo_input={"small": 1, "mid": 10, "huge": 9000},
        cents_per_kilo_output={"small": 2, "mid": 20, "huge": 9000},
    )


def _gate(cap_cents: int) -> BudgetGate:
    tracker = SpendTracker(now=lambda: datetime(2026, 5, 29, 12, tzinfo=UTC))
    return BudgetGate(daily_cap_cents=cap_cents, spend_tracker=tracker, price_table=_prices())


def _bt(name: str, model: str, size_b: float) -> BudgetedTeacher:
    return BudgetedTeacher(
        candidate=TeacherCandidate(name=name, size_b=size_b),
        model=model,
        est_input_tokens=1000,
        est_output_tokens=1000,
    )


def _harness(gate: BudgetGate, scores: dict[str, EvalRun]) -> BudgetedTeacherAB:
    def polish(teacher: TeacherCandidate, curated):
        return [
            SFTCandidate(
                question=c.question,
                reasoning=c.reasoning,
                answer=f"{teacher.name}:{c.answer}",
                specialty=c.specialty,
            )
            for c in curated
        ]

    def train_and_eval(polished):
        return scores[polished[0].answer.split(":", 1)[0]]

    return BudgetedTeacherAB(polish=polish, train_and_eval=train_and_eval, budget_gate=gate)


def _run(score: float) -> EvalRun:
    return EvalRun(scores=(score, score), outputs=("alpha beta", "gamma delta"))


def test_affordable_teachers_run_and_best_transfer_wins() -> None:
    gate = _gate(cap_cents=1000)  # $10
    ab = _harness(gate, {"small": _run(0.6), "mid": _run(0.8)})

    result = ab.run(teachers=[_bt("small", "small", 7.0), _bt("mid", "mid", 32.0)], curated=CURATED)

    assert result.skipped_for_budget == ()
    assert result.report is not None
    assert result.report.winner.teacher.name == "mid"  # best student transfer
    assert result.spent_cents > 0


def test_over_budget_teacher_is_skipped_not_run() -> None:
    gate = _gate(cap_cents=1000)  # $10 — "huge" at 9000c/kilo blows past it
    ab = _harness(gate, {"small": _run(0.6), "huge": _run(0.99)})

    result = ab.run(
        teachers=[_bt("small", "small", 7.0), _bt("huge", "huge", 405.0)], curated=CURATED
    )

    # The huge teacher would have "won" on score, but it never ran — skipped for
    # budget — so the affordable small teacher is the winner.
    assert result.skipped_for_budget == ("huge",)
    assert result.report is not None
    assert result.report.winner.teacher.name == "small"


def test_all_over_budget_raises() -> None:
    gate = _gate(cap_cents=5)  # $0.05 — nothing is affordable
    ab = _harness(gate, {"mid": _run(0.8)})

    with pytest.raises(AllTeachersOverBudgetError):
        ab.run(teachers=[_bt("mid", "mid", 32.0)], curated=CURATED)


def test_spend_is_recorded_against_the_tracker() -> None:
    gate = _gate(cap_cents=1000)
    remaining_before = gate.remaining_cents()
    ab = _harness(gate, {"small": _run(0.6)})

    result = ab.run(teachers=[_bt("small", "small", 7.0)], curated=CURATED)

    assert gate.remaining_cents() == remaining_before - result.spent_cents
    assert result.spent_cents > 0
