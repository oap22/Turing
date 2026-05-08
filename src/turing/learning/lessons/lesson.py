"""Lesson — what the cluster learned from a closed episode.

Lessons are short, structured, specialty-tagged. They sit in a vector store
keyed by their text embedding so subsequent subtasks can retrieve the most
relevant ones for their specialty and inject them into the worker prompt.

The schema is versioned so an old extractor or store can refuse a payload it
cannot fully understand instead of silently mixing formats.

Schema v2 (ADR 0005 §4) adds ``created_at_ms`` and ``pinned_until_ms`` for
the 60-day TTL + pin-renewal lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CURRENT_LESSON_SCHEMA_VERSION = 2


class LessonSchemaError(Exception):
    """Raised when a lesson dict has an unsupported schema_version."""


@dataclass(frozen=True)
class Lesson:
    specialty: str
    task_id: str
    text: str
    embedding: tuple[float, ...]
    # ADR 0005 §4: timestamps for the 60-day TTL + pin-renewal lifecycle.
    # Default 0 lets older test fixtures construct lessons without timestamps;
    # the runtime path always passes a real ``created_at_ms`` from the
    # nightly extraction job.
    created_at_ms: int = 0
    # ``None`` = unpinned (subject to TTL eviction). Set by
    # ``LessonStore.pin`` when prompt_evolution.evolver cites the lesson
    # in an A/B-winning prompt; renewal extends the timestamp.
    pinned_until_ms: int | None = None
    schema_version: int = CURRENT_LESSON_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "specialty": self.specialty,
            "task_id": self.task_id,
            "text": self.text,
            "embedding": list(self.embedding),
            "created_at_ms": self.created_at_ms,
            "pinned_until_ms": self.pinned_until_ms,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Lesson:
        version = raw.get("schema_version")
        if version != CURRENT_LESSON_SCHEMA_VERSION:
            raise LessonSchemaError(
                f"lesson schema_version {version!r} does not match "
                f"{CURRENT_LESSON_SCHEMA_VERSION}"
            )
        pinned_raw = raw.get("pinned_until_ms")
        return cls(
            specialty=str(raw["specialty"]),
            task_id=str(raw["task_id"]),
            text=str(raw["text"]),
            embedding=tuple(float(x) for x in raw["embedding"]),
            created_at_ms=int(raw.get("created_at_ms", 0)),
            pinned_until_ms=None if pinned_raw is None else int(pinned_raw),
            schema_version=int(version),
        )
