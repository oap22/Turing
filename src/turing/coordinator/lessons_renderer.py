"""Lesson-injection renderer for SubtaskDispatch.prompt.

Issue #119 / ADR 0005 §3. Coordinator-side path: before building a
`SubtaskDispatch`, embed the subtask prompt, query `LessonStore` for the
top-k lessons matching the worker's specialty, and prepend them via
`inject_lessons_into_prompt`. Empty-store / no-match → prompt unchanged.

Workers stay pure executors — they never reach back into LessonStore.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from turing.learning.lessons.injector import inject_lessons_into_prompt

if TYPE_CHECKING:
    from turing.learning.lessons.store import LessonStore


class _EmbedderLike(Protocol):
    def embed(self, text: str): ...


def _to_float_tuple(vec) -> tuple[float, ...]:
    return tuple(float(x) for x in vec)


def render_prompt_with_lessons(
    *,
    base_prompt: str,
    specialty: str,
    store: LessonStore | None,
    embedder: _EmbedderLike | None,
    k: int = 3,
) -> str:
    """Return the prompt with up-to-`k` matching lessons prepended.

    If either `store` or `embedder` is None the prompt passes through
    unchanged — gives callers a single config-driven on/off knob.
    """
    if store is None or embedder is None or k <= 0:
        return base_prompt
    embedding = _to_float_tuple(embedder.embed(base_prompt))
    lessons = store.query(specialty=specialty, embedding=embedding, k=k)
    return inject_lessons_into_prompt(base_prompt=base_prompt, lessons=lessons)
