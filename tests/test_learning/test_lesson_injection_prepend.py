"""inject_lessons_into_prompt prepends per ADR 0005 §3 / issue #105 spec.

The user's prompt is the *trunk*; the injected lessons are *prefix
context* the worker should read before encountering the actual ask.
Prepending matches the issue spec format:

    <lessons>
    - lesson 1
    - lesson 2
    </lessons>
    <original user prompt>

Empty lessons list still short-circuits — no empty <lessons> wrappers
appear in the prompt.
"""

from __future__ import annotations

from turing.learning.lessons import Lesson, inject_lessons_into_prompt


def _lesson(text: str) -> Lesson:
    return Lesson(
        specialty="research-summarize",
        task_id=f"task-{text}",
        text=text,
        embedding=(0.1, 0.2),
    )


def test_lessons_prepended_to_user_message() -> None:
    rendered = inject_lessons_into_prompt(
        base_prompt="please summarise paper X",
        lessons=[_lesson("prefer tables for X-vs-Y")],
    )
    # <lessons> block precedes the user prompt.
    lessons_idx = rendered.index("<lessons>")
    user_idx = rendered.index("please summarise")
    assert lessons_idx < user_idx


def test_block_format_matches_issue_spec() -> None:
    """One bullet per lesson inside <lessons> ... </lessons>."""
    rendered = inject_lessons_into_prompt(
        base_prompt="prompt",
        lessons=[_lesson("A"), _lesson("B"), _lesson("C")],
    )
    assert "<lessons>" in rendered
    assert "</lessons>" in rendered
    assert "- A" in rendered
    assert "- B" in rendered
    assert "- C" in rendered
    # Lessons block ends before user prompt.
    end_block = rendered.index("</lessons>")
    user_idx = rendered.index("prompt")
    assert end_block < user_idx


def test_empty_lessons_returns_base_prompt_unchanged() -> None:
    out = inject_lessons_into_prompt(base_prompt="hello", lessons=[])
    assert out == "hello"
    assert "<lessons>" not in out


def test_user_prompt_text_preserved_verbatim() -> None:
    base = "do the thing exactly as I said"
    rendered = inject_lessons_into_prompt(
        base_prompt=base, lessons=[_lesson("never mention x")]
    )
    # Original prompt appears verbatim somewhere after the lessons block.
    assert base in rendered
