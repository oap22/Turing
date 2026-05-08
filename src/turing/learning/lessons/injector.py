"""inject_lessons_into_prompt — prepend top-k lessons in a <lessons> block.

ADR 0005 §3 + issue #105: the user prompt is the trunk; injected lessons
are *prefix context* the worker should read before encountering the
actual ask. Format:

    <lessons>
    - lesson 1
    - lesson 2
    </lessons>
    <original user prompt>

Empty lessons list short-circuits — no empty <lessons> wrappers appear
in the prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from turing.learning.lessons.lesson import Lesson


def inject_lessons_into_prompt(
    *,
    base_prompt: str,
    lessons: Sequence[Lesson],
) -> str:
    if not lessons:
        return base_prompt
    body = "\n".join(f"- {lesson.text}" for lesson in lessons)
    block = f"<lessons>\n{body}\n</lessons>\n\n"
    return block + base_prompt
