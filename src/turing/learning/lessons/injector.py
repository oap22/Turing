"""inject_lessons_into_prompt — render top-k lessons under <lessons>...</lessons>.

The clearly-labelled section makes it visible to the operator (and easy to
diff in eval fixtures) that retrieved lessons changed the worker prompt.
An empty list short-circuits — no empty <lessons> tags appear in the prompt.
"""

from __future__ import annotations

from collections.abc import Sequence

from turing.learning.lessons.lesson import Lesson


def inject_lessons_into_prompt(
    *,
    base_prompt: str,
    lessons: Sequence[Lesson],
) -> str:
    if not lessons:
        return base_prompt
    body = "\n".join(f"- {l.text}" for l in lessons)
    block = f"\n\n<lessons>\n{body}\n</lessons>"
    return base_prompt + block
