"""LessonStore — in-memory vector index of lessons keyed by specialty.

The store is intentionally tiny: a flat list filtered by specialty, scored by
cosine similarity, sorted top-k. The hot path is dominated by extractor LLM
calls and worker dispatch, so a brute-force scan is fine until the cluster
size makes it not — at which point the storage moves into ``vault.index``'s
sqlite-vec backend (out of scope for this slice).

ADR 0005 lifecycle:
- ``pin(lesson_ids, until_ms)`` is the single inbound pin API; called by
  ``prompt_evolution.evolver`` on A/B-winning prompts. Renewal extends
  ``pinned_until_ms`` only forward; earlier values are no-ops.
- ``evict_expired(now_ms, ttl_ms)`` runs in the nightly window. Removes
  lessons whose ``created_at_ms`` exceeded the TTL AND whose
  ``pinned_until_ms`` has lapsed (or is None).
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from turing.learning.lessons.lesson import Lesson


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b, strict=False))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    if da == 0.0 or db == 0.0:
        return 0.0
    return num / (da * db)


class LessonStore:
    def __init__(self) -> None:
        self._lessons: list[Lesson] = []

    def add(self, lesson: Lesson) -> None:
        self._lessons.append(lesson)

    def pinned_until(self, task_id: str) -> int | None:
        for lesson in self._lessons:
            if lesson.task_id == task_id:
                return lesson.pinned_until_ms
        return None

    def query(
        self,
        *,
        specialty: str,
        embedding: tuple[float, ...],
        k: int,
    ) -> list[Lesson]:
        candidates = [lesson for lesson in self._lessons if lesson.specialty == specialty]
        scored = [(_cosine(embedding, lesson.embedding), lesson) for lesson in candidates]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [lesson for _, lesson in scored[: max(0, k)]]

    def get_by_task_id(self, task_id: str) -> Lesson:
        """Return the lesson with this task_id; raise ``KeyError`` if absent."""
        for lesson in self._lessons:
            if lesson.task_id == task_id:
                return lesson
        raise KeyError(f"no lesson with task_id={task_id!r}")

    def pin(self, *, lesson_ids: Iterable[str], until_ms: int) -> None:
        """Set or extend ``pinned_until_ms`` for each known ``lesson_id``.

        Renewal semantics (ADR 0005 §3): only extends forward. A pin call
        with an earlier ``until_ms`` than the current value is a no-op.
        Unknown lesson_ids are silently skipped — the evolver may cite a
        lesson that's been evicted between extraction and prompt-emit.
        """
        targets = set(lesson_ids)
        for i, lesson in enumerate(self._lessons):
            if lesson.task_id not in targets:
                continue
            if lesson.pinned_until_ms is None or until_ms > lesson.pinned_until_ms:
                self._lessons[i] = replace(lesson, pinned_until_ms=until_ms)

    def evict_expired(self, *, now_ms: int, ttl_ms: int) -> int:
        """Remove lessons whose TTL has elapsed and whose pin has lapsed.

        Returns the number of lessons evicted. Pinned-and-not-yet-lapsed
        lessons are TTL-exempt per ADR 0005 §3.
        """
        cutoff = now_ms - ttl_ms
        before = len(self._lessons)
        self._lessons = [
            lesson
            for lesson in self._lessons
            if not (
                lesson.created_at_ms < cutoff
                and (lesson.pinned_until_ms is None or lesson.pinned_until_ms < now_ms)
            )
        ]
        return before - len(self._lessons)

    def __len__(self) -> int:
        return len(self._lessons)
