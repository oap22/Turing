"""Tests for the teacher/polisher A/B sizing harness (ADR 0009 §4).

The winner is chosen by *student transfer* (held-out score), not teacher size —
the "Larger Models' Paradox". Size is only a smaller-is-better tie-breaker.
"""

from __future__ import annotations

import pytest

from turing.coordinator.flywheel.morning_curation import SFTCandidate
from turing.learning.eval_set.collapse_gate import EvalRun
from turing.learning.trainer import (
    NoTeacherArmsError,
    TeacherABHarness,
    TeacherCandidate,
)

CURATED = [SFTCandidate(question="q", reasoning="r", answer="a", specialty="ai-ml-generalist")]


def _harness(scores: dict[str, EvalRun]) -> TeacherABHarness:
    """Build a harness whose train_and_eval returns a canned EvalRun per teacher.

    The polisher tags each candidate's answer with the teacher name so the
    train_and_eval stub can look up that teacher's scripted student result.
    """

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
        teacher_name = polished[0].answer.split(":", 1)[0]
        return scores[teacher_name]

    return TeacherABHarness(polish=polish, train_and_eval=train_and_eval)


def _run(score: float, outputs: tuple[str, ...] = ("alpha beta", "gamma delta")) -> EvalRun:
    return EvalRun(scores=(score,) * len(outputs), outputs=outputs)


def test_picks_best_student_transfer_not_biggest_teacher() -> None:
    teachers = [
        TeacherCandidate("small-8b", 8.0),
        TeacherCandidate("mid-32b", 32.0),
        TeacherCandidate("huge-405b", 405.0),
    ]
    # The mid-size teacher transfers best; the huge one does NOT win.
    harness = _harness(
        {
            "small-8b": _run(0.60),
            "mid-32b": _run(0.80),
            "huge-405b": _run(0.65),
        }
    )

    report = harness.run(teachers=teachers, curated=CURATED)

    assert report.winner.teacher.name == "mid-32b"
    assert report.winner.student_score == pytest.approx(0.80)
    # Ranked best-first.
    assert [a.teacher.name for a in report.arms] == ["mid-32b", "huge-405b", "small-8b"]


def test_tie_breaks_toward_smaller_teacher() -> None:
    teachers = [TeacherCandidate("big", 70.0), TeacherCandidate("small", 7.0)]
    harness = _harness({"big": _run(0.75), "small": _run(0.75)})  # exact tie

    report = harness.run(teachers=teachers, curated=CURATED)

    assert report.winner.teacher.name == "small"  # cheaper wins the tie
    assert report.winner_is_smallest


def test_winner_is_smallest_flag_false_when_larger_wins() -> None:
    teachers = [TeacherCandidate("small", 7.0), TeacherCandidate("mid", 32.0)]
    harness = _harness({"small": _run(0.50), "mid": _run(0.90)})

    report = harness.run(teachers=teachers, curated=CURATED)

    assert report.winner.teacher.name == "mid"
    assert not report.winner_is_smallest


def test_report_records_student_diversity_and_tail() -> None:
    teachers = [TeacherCandidate("t", 13.0)]
    run = EvalRun(scores=(1.0, 0.0), outputs=("alpha beta", "gamma delta"))
    harness = _harness({"t": run})

    report = harness.run(teachers=teachers, curated=CURATED)

    arm = report.winner
    assert arm.student_tail == pytest.approx(0.5)  # one of two cases scored > 0
    assert arm.student_diversity == pytest.approx(run.diversity)


def test_empty_teacher_list_raises() -> None:
    harness = _harness({})
    with pytest.raises(NoTeacherArmsError):
        harness.run(teachers=[], curated=CURATED)
