"""Lessons-extractor + auto-injection pipeline."""

from __future__ import annotations

from turing.learning.lessons.extractor import LessonExtractor
from turing.learning.lessons.injector import inject_lessons_into_prompt
from turing.learning.lessons.lesson import (
    CURRENT_LESSON_SCHEMA_VERSION,
    Lesson,
    LessonSchemaError,
)
from turing.learning.lessons.store import LessonStore

__all__ = [
    "CURRENT_LESSON_SCHEMA_VERSION",
    "Lesson",
    "LessonExtractor",
    "LessonSchemaError",
    "LessonStore",
    "inject_lessons_into_prompt",
]
