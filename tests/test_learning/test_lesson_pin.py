"""LessonStore.pin — single inbound pin API per ADR 0005 §5.

prompt_evolution.evolver calls pin(lesson_ids, until_ms) when an A/B-winning
prompt is promoted. Renewal semantics: set pinned_until_ms if greater than
the current value (extending always wins; rolling back never does).
"""

from __future__ import annotations

from turing.learning.lessons import Lesson, LessonStore


def _lesson(
    *,
    text: str = "lesson",
    specialty: str = "research-summarize",
    created_at_ms: int = 1000,
) -> Lesson:
    return Lesson(
        specialty=specialty,
        task_id=f"task-{text}",
        text=text,
        embedding=(0.1, 0.2),
        created_at_ms=created_at_ms,
        pinned_until_ms=None,
    )


def test_pin_an_unpinned_lesson_sets_pinned_until_ms() -> None:
    store = LessonStore()
    lesson = _lesson(text="A")
    store.add(lesson)

    store.pin(lesson_ids=[lesson.task_id], until_ms=10_000)

    pinned = store.get_by_task_id(lesson.task_id)
    assert pinned.pinned_until_ms == 10_000


def test_pin_renewal_extends_pinned_until_ms() -> None:
    """Calling pin again with a later until_ms extends."""
    store = LessonStore()
    lesson = _lesson(text="A")
    store.add(lesson)
    store.pin(lesson_ids=[lesson.task_id], until_ms=10_000)

    store.pin(lesson_ids=[lesson.task_id], until_ms=20_000)

    assert store.get_by_task_id(lesson.task_id).pinned_until_ms == 20_000


def test_pin_with_earlier_until_ms_is_noop() -> None:
    """Calling pin with an earlier until_ms must not roll back."""
    store = LessonStore()
    lesson = _lesson(text="A")
    store.add(lesson)
    store.pin(lesson_ids=[lesson.task_id], until_ms=10_000)

    store.pin(lesson_ids=[lesson.task_id], until_ms=5_000)

    assert store.get_by_task_id(lesson.task_id).pinned_until_ms == 10_000


def test_pin_unknown_lesson_id_is_silently_ignored() -> None:
    """The evolver may cite a lesson_id that's been evicted between
    extraction and prompt-emit. Silent skip is the contract — pin is
    best-effort, not assertive."""
    store = LessonStore()
    store.add(_lesson(text="A"))
    # Should not raise.
    store.pin(lesson_ids=["task-nonexistent"], until_ms=10_000)


def test_pin_multiple_ids_in_one_call() -> None:
    store = LessonStore()
    a = _lesson(text="A")
    b = _lesson(text="B")
    c = _lesson(text="C")
    for lesson in (a, b, c):
        store.add(lesson)

    store.pin(lesson_ids=[a.task_id, b.task_id], until_ms=10_000)

    assert store.get_by_task_id(a.task_id).pinned_until_ms == 10_000
    assert store.get_by_task_id(b.task_id).pinned_until_ms == 10_000
    assert store.get_by_task_id(c.task_id).pinned_until_ms is None
