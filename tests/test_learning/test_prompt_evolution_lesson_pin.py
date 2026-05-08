"""PromptEvolver pin contract with LessonStore (issue #114, ADR 0005 §5)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.learning.lessons import Lesson, LessonStore
from turing.learning.prompt_evolution import (
    PromotionDecision,
    PromptEvolver,
    PromptVersionRegistry,
)
from turing.learning.prompt_evolution.evolver import LESSON_PIN_DURATION_MS


def _lesson(task_id: str, specialty: str = "research-summarize") -> Lesson:
    return Lesson(
        specialty=specialty,
        task_id=task_id,
        text=f"lesson {task_id}",
        embedding=(0.1, 0.2, 0.3),
    )


def _evolver(
    *,
    lesson_store: LessonStore | None = None,
    now_ms: int = 1_000_000,
) -> PromptEvolver:
    pattern_extractor = MagicMock()
    pattern_extractor.extract = AsyncMock(return_value={})
    return PromptEvolver(
        episode_store=EpisodeStore(),
        registry=PromptVersionRegistry(),
        pattern_extractor=pattern_extractor,
        lesson_store=lesson_store,
        now_ms=lambda: now_ms,
    )


def test_lesson_pin_duration_is_60_days() -> None:
    assert LESSON_PIN_DURATION_MS == 60 * 24 * 3600 * 1000


def test_records_cited_lesson_ids_per_specialty() -> None:
    ev = _evolver()
    ev.record_cited_lessons("research-summarize", ["t-1", "t-2"])
    ev.record_cited_lessons("other", ["t-9"])
    assert ev.cited_lessons("research-summarize") == ["t-1", "t-2"]
    assert ev.cited_lessons("other") == ["t-9"]


def test_promote_pins_cited_lessons() -> None:
    store = LessonStore()
    for tid in ("t-1", "t-2", "t-3"):
        store.add(_lesson(tid))
    ev = _evolver(lesson_store=store, now_ms=1_000_000)
    ev._registry.set_baseline("research-summarize", "v1")
    ev._registry.set_candidate("research-summarize", "v2")
    ev.record_cited_lessons("research-summarize", ["t-1", "t-2"])

    ev.apply_decision("research-summarize", PromotionDecision.PROMOTE)

    assert ev._registry.baseline_for("research-summarize") == "v2"
    assert store.pinned_until("t-1") == 1_000_000 + LESSON_PIN_DURATION_MS
    assert store.pinned_until("t-2") == 1_000_000 + LESSON_PIN_DURATION_MS
    assert store.pinned_until("t-3") is None


def test_reject_does_not_pin() -> None:
    store = LessonStore()
    for tid in ("t-1", "t-2", "t-3"):
        store.add(_lesson(tid))
    ev = _evolver(lesson_store=store)
    ev._registry.set_baseline("research-summarize", "v1")
    ev._registry.set_candidate("research-summarize", "v2")
    ev.record_cited_lessons("research-summarize", ["t-1", "t-2"])

    ev.apply_decision("research-summarize", PromotionDecision.REJECT)

    assert store.pinned_until("t-1") is None
    assert store.pinned_until("t-2") is None
    assert store.pinned_until("t-3") is None


def test_hold_does_not_pin() -> None:
    store = LessonStore()
    store.add(_lesson("t-1"))
    ev = _evolver(lesson_store=store)
    ev._registry.set_baseline("research-summarize", "v1")
    ev._registry.set_candidate("research-summarize", "v2")
    ev.record_cited_lessons("research-summarize", ["t-1"])

    ev.apply_decision("research-summarize", PromotionDecision.HOLD_INSUFFICIENT_DATA)

    assert store.pinned_until("t-1") is None


def test_pin_failure_does_not_break_promotion() -> None:
    class BoomStore:
        def pin(self, *, lesson_ids, until_ms):
            raise RuntimeError("disk on fire")

    ev = _evolver(lesson_store=BoomStore())
    ev._registry.set_baseline("research-summarize", "v1")
    ev._registry.set_candidate("research-summarize", "v2")
    ev.record_cited_lessons("research-summarize", ["t-1"])

    ev.apply_decision("research-summarize", PromotionDecision.PROMOTE)

    assert ev._registry.baseline_for("research-summarize") == "v2"


def test_promote_with_no_lesson_store_is_noop_safe() -> None:
    ev = _evolver(lesson_store=None)
    ev._registry.set_baseline("research-summarize", "v1")
    ev._registry.set_candidate("research-summarize", "v2")
    ev.record_cited_lessons("research-summarize", ["t-1"])

    ev.apply_decision("research-summarize", PromotionDecision.PROMOTE)

    assert ev._registry.baseline_for("research-summarize") == "v2"


def test_lesson_store_pin_idempotent_and_independent() -> None:
    store = LessonStore()
    store.add(_lesson("t-1"))
    store.add(_lesson("t-2"))

    store.pin(lesson_ids=["t-1"], until_ms=5000)
    store.pin(lesson_ids=["t-1"], until_ms=9000)
    assert store.pinned_until("t-1") == 9000
    assert store.pinned_until("t-2") is None
