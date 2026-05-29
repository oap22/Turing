"""Teacher/polisher A/B sizing harness (ADR 0009 §4).

The last §4 guardrail: **A/B the polisher/teacher size; biggest is not always
best for a 7-8 B student.** Teacher-student *compatibility* matters more than
raw teacher capability — a mid-size teacher can transfer better than the largest
one ("Larger Models' Paradox", Stronger Models are NOT Stronger Teachers,
arXiv:2411.07133). So the teacher must be chosen by **measured student transfer
on held-out real eval**, never by teacher size.

This harness runs each candidate teacher through one polish→train→eval arm and
picks the winner by the student's held-out score (the
:class:`~turing.learning.eval_set.collapse_gate.EvalRun` mean), with teacher size
used only as a **tie-breaker toward the smaller** teacher (cheaper, and the
paradox says bigger isn't reliably better). Polishing, training, and evaluating
are all injected so the harness is exercised without a GPU or live model.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from turing.coordinator.flywheel.morning_curation import SFTCandidate
    from turing.learning.eval_set.collapse_gate import EvalRun


@dataclass(frozen=True)
class TeacherCandidate:
    """One teacher/polisher under test.

    ``size_b`` is the teacher's parameter count in billions — used *only* as a
    smaller-is-better tie-breaker, never as the primary selection signal.
    """

    name: str
    size_b: float


# A polisher turns the raw curated pairs into teacher-polished training targets.
Polisher = Callable[["TeacherCandidate", "Sequence[SFTCandidate]"], "Sequence[SFTCandidate]"]
# A train-and-eval arm returns the student's held-out EvalRun after SFT on the
# polished pairs. Injected so the harness needs no GPU.
TrainEval = Callable[["Sequence[SFTCandidate]"], "EvalRun"]


@dataclass(frozen=True)
class TeacherArmResult:
    """The measured outcome of one teacher arm."""

    teacher: TeacherCandidate
    student_score: float
    student_diversity: float
    student_tail: float

    @classmethod
    def from_eval(cls, teacher: TeacherCandidate, run: EvalRun) -> TeacherArmResult:
        return cls(
            teacher=teacher,
            student_score=run.mean_score,
            student_diversity=run.diversity,
            student_tail=run.tail_coverage,
        )


@dataclass(frozen=True)
class TeacherABReport:
    """Ranked arms + the chosen teacher, for the operator's record."""

    arms: tuple[TeacherArmResult, ...]
    winner: TeacherArmResult

    @property
    def winner_is_smallest(self) -> bool:
        """True when the winning teacher is the smallest one tested — the case
        the paradox predicts and the reason size alone must not pick."""
        return self.winner.teacher.size_b == min(a.teacher.size_b for a in self.arms)


class NoTeacherArmsError(ValueError):
    """Raised when an A/B run is asked to choose among zero teachers."""


class TeacherABHarness:
    """Runs each candidate teacher's polish→train→eval arm and picks by transfer."""

    def __init__(
        self,
        *,
        polish: Polisher,
        train_and_eval: TrainEval,
    ) -> None:
        self._polish = polish
        self._train_and_eval = train_and_eval

    def run(
        self,
        *,
        teachers: Sequence[TeacherCandidate],
        curated: Sequence[SFTCandidate],
    ) -> TeacherABReport:
        """A/B every teacher; choose the one with the best *student* transfer.

        Selection key: highest held-out student score; ties broken toward the
        **smaller** teacher (cheaper, and bigger is not reliably better). Teacher
        size is never the primary signal.
        """
        if not teachers:
            raise NoTeacherArmsError("at least one teacher candidate is required")

        arms: list[TeacherArmResult] = []
        for teacher in teachers:
            polished = self._polish(teacher, curated)
            run = self._train_and_eval(polished)
            arms.append(TeacherArmResult.from_eval(teacher, run))

        # Primary: student score (desc). Tie-break: smaller teacher (asc size).
        winner = max(arms, key=lambda a: (a.student_score, -a.teacher.size_b))
        # Stable, readable ranking for the report.
        ranked = sorted(arms, key=lambda a: (a.student_score, -a.teacher.size_b), reverse=True)
        return TeacherABReport(arms=tuple(ranked), winner=winner)
