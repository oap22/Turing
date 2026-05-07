"""LessonStore — in-memory vector index of lessons keyed by specialty.

The store is intentionally tiny: a flat list filtered by specialty, scored by
cosine similarity, sorted top-k. The hot path is dominated by extractor LLM
calls and worker dispatch, so a brute-force scan is fine until the cluster
size makes it not — at which point the storage moves into ``vault.index``'s
sqlite-vec backend (out of scope for this slice).
"""

from __future__ import annotations

import math

from turing.learning.lessons.lesson import Lesson


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
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

    def query(
        self,
        *,
        specialty: str,
        embedding: tuple[float, ...],
        k: int,
    ) -> list[Lesson]:
        candidates = [l for l in self._lessons if l.specialty == specialty]
        scored = [(_cosine(embedding, l.embedding), l) for l in candidates]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [l for _, l in scored[: max(0, k)]]

    def __len__(self) -> int:
        return len(self._lessons)
