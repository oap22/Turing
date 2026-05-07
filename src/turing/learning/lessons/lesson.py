"""Lesson — what the cluster learned from a closed episode.

Lessons are short, structured, specialty-tagged. They sit in a vector store
keyed by their text embedding so subsequent subtasks can retrieve the most
relevant ones for their specialty and inject them into the worker prompt.

The schema is versioned so an old extractor or store can refuse a payload it
cannot fully understand instead of silently mixing formats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CURRENT_LESSON_SCHEMA_VERSION = 1


class LessonSchemaError(Exception):
    """Raised when a lesson dict has an unsupported schema_version."""


@dataclass(frozen=True)
class Lesson:
    specialty: str
    task_id: str
    text: str
    embedding: tuple[float, ...]
    schema_version: int = CURRENT_LESSON_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "specialty": self.specialty,
            "task_id": self.task_id,
            "text": self.text,
            "embedding": list(self.embedding),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Lesson":
        version = raw.get("schema_version")
        if version != CURRENT_LESSON_SCHEMA_VERSION:
            raise LessonSchemaError(
                f"lesson schema_version {version!r} does not match "
                f"{CURRENT_LESSON_SCHEMA_VERSION}"
            )
        return cls(
            specialty=str(raw["specialty"]),
            task_id=str(raw["task_id"]),
            text=str(raw["text"]),
            embedding=tuple(float(x) for x in raw["embedding"]),
            schema_version=int(version),
        )
