"""Lesson schema v2 — adds created_at_ms and pinned_until_ms per ADR 0005 §4.

CURRENT_LESSON_SCHEMA_VERSION → 2. v1 payloads refuse to load (LessonSchemaError);
v2 round-trips through to_dict/from_dict.
"""

from __future__ import annotations

import pytest

from turing.learning.lessons.lesson import (
    CURRENT_LESSON_SCHEMA_VERSION,
    Lesson,
    LessonSchemaError,
)


def test_schema_version_is_2() -> None:
    assert CURRENT_LESSON_SCHEMA_VERSION == 2


def test_lesson_carries_created_at_ms_and_pinned_until_ms() -> None:
    lesson = Lesson(
        specialty="research-summarize",
        task_id="task-1",
        text="prefer side-by-side tables for X-vs-Y prompts",
        embedding=(0.1, 0.2, 0.3),
        created_at_ms=1_000_000,
        pinned_until_ms=None,
    )
    assert lesson.created_at_ms == 1_000_000
    assert lesson.pinned_until_ms is None


def test_v2_round_trips_through_dict() -> None:
    lesson = Lesson(
        specialty="research-deep",
        task_id="task-2",
        text="cite primary sources where available",
        embedding=(0.0, 1.0),
        created_at_ms=42,
        pinned_until_ms=84,
    )
    payload = lesson.to_dict()
    assert payload["schema_version"] == 2
    assert payload["created_at_ms"] == 42
    assert payload["pinned_until_ms"] == 84

    again = Lesson.from_dict(payload)
    assert again == lesson


def test_v1_payload_is_refused() -> None:
    """Old extractor / old store payloads with schema_version=1 must
    refuse rather than silently mix formats."""
    v1_payload = {
        "schema_version": 1,
        "specialty": "research-summarize",
        "task_id": "task-1",
        "text": "old lesson",
        "embedding": [0.0, 0.0],
    }
    with pytest.raises(LessonSchemaError):
        Lesson.from_dict(v1_payload)


def test_pinned_until_ms_defaults_to_none() -> None:
    """Newly extracted lessons start unpinned."""
    lesson = Lesson(
        specialty="x",
        task_id="t",
        text="t",
        embedding=(0.0,),
        created_at_ms=1,
    )
    assert lesson.pinned_until_ms is None
