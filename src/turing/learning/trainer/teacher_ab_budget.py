"""Budget-gated teacher/polisher A/B (ADR 0009 §4, issue #270).

The merged :class:`~turing.learning.trainer.teacher_ab.TeacherABHarness` picks a
teacher by measured *student transfer*. Issue #270 adds the missing operational
guardrail: the polish step calls a **cloud** teacher (Claude), so each arm's
spend must pass the **$10/day** :class:`~turing.coordinator.budget.budget_gate.BudgetGate`
before it runs, and the actual spend is recorded against the tracker.

This wrapper sits in front of the harness's per-teacher polish call:

- Before polishing with a teacher, estimate the cloud spend (tokens × the
  teacher's model price) and ask the :class:`BudgetGate`. If the gate returns
  ``FALLBACK_LOCAL`` (the call would breach today's cap), that teacher arm is
  **skipped** — never silently run over budget.
- After a gated polish, record the actual spend so later arms (and the rest of
  the day's cloud calls) see the reduced remaining budget.
- The surviving arms are handed to the existing harness, which selects by
  student transfer exactly as before.

Polishing and train+eval stay injected, so the A/B runs without a GPU or live
cloud calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from turing.learning.trainer.teacher_ab import (
    NoTeacherArmsError,
    TeacherABHarness,
    TeacherABReport,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from turing.coordinator.budget.budget_gate import BudgetGate
    from turing.coordinator.flywheel.morning_curation import SFTCandidate
    from turing.learning.eval_set.collapse_gate import EvalRun
    from turing.learning.trainer.teacher_ab import TeacherCandidate


@dataclass(frozen=True)
class BudgetedTeacher:
    """A teacher candidate plus the cloud cost knobs for its polish pass.

    ``model`` keys the :class:`~turing.coordinator.budget.budget_gate.ModelPriceTable`;
    ``est_input_tokens`` / ``est_output_tokens`` are the per-polish-pass token
    estimate the gate prices.
    """

    candidate: TeacherCandidate
    model: str
    est_input_tokens: int
    est_output_tokens: int


@dataclass(frozen=True)
class BudgetedTeacherABReport:
    """The A/B outcome plus which teachers were skipped for budget."""

    report: TeacherABReport | None
    skipped_for_budget: tuple[str, ...]
    spent_cents: int


class AllTeachersOverBudgetError(RuntimeError):
    """Raised when every teacher arm would breach the daily budget cap."""


class BudgetedTeacherAB:
    """Runs the teacher A/B with each arm's cloud polish gated by the budget."""

    def __init__(
        self,
        *,
        polish: Callable[[TeacherCandidate, Sequence[SFTCandidate]], Sequence[SFTCandidate]],
        train_and_eval: Callable[[Sequence[SFTCandidate]], EvalRun],
        budget_gate: BudgetGate,
        task_id: str = "teacher-ab",
    ) -> None:
        self._polish = polish
        self._train_and_eval = train_and_eval
        self._gate = budget_gate
        self._task_id = task_id

    def run(
        self,
        *,
        teachers: Sequence[BudgetedTeacher],
        curated: Sequence[SFTCandidate],
    ) -> BudgetedTeacherABReport:
        """A/B the affordable teachers; pick by student transfer.

        Each teacher's polish spend is checked against the budget gate *before*
        the arm runs; unaffordable teachers are skipped (reported, not run).
        Raises :class:`AllTeachersOverBudgetError` if none are affordable.
        """
        if not teachers:
            raise NoTeacherArmsError("at least one teacher candidate is required")

        affordable: list[TeacherCandidate] = []
        skipped: list[str] = []
        spent = 0
        # Wrap the injected polisher so the harness's per-arm polish call is the
        # one that has already passed the gate (and is recorded as actual spend).
        for bt in teachers:
            decision = self._gate.check(
                task_id=self._task_id,
                model=bt.model,
                input_tokens=bt.est_input_tokens,
                output_tokens=bt.est_output_tokens,
            )
            if decision.action.value != "approve":
                skipped.append(bt.candidate.name)
                continue
            affordable.append(bt.candidate)
            spent += self._gate.record_actual(
                task_id=self._task_id,
                model=bt.model,
                input_tokens=bt.est_input_tokens,
                output_tokens=bt.est_output_tokens,
            )

        if not affordable:
            raise AllTeachersOverBudgetError(
                "every teacher arm would breach the daily budget cap; "
                f"remaining ${self._gate.remaining_cents() / 100:.2f}"
            )

        harness = TeacherABHarness(polish=self._polish, train_and_eval=self._train_and_eval)
        report = harness.run(teachers=affordable, curated=curated)
        return BudgetedTeacherABReport(
            report=report,
            skipped_for_budget=tuple(skipped),
            spent_cents=spent,
        )
