"""Coordinator-side lesson injection at dispatch-build time (issue #119)."""

from __future__ import annotations

from turing.coordinator.lessons_renderer import render_prompt_with_lessons
from turing.learning.lessons import Lesson, LessonStore
from turing.vault.embedder import DeterministicHashEmbedder


def _lesson(*, specialty: str, task_id: str, text: str, embedder) -> Lesson:
    emb = embedder.embed(text)
    return Lesson(
        specialty=specialty,
        task_id=task_id,
        text=text,
        embedding=tuple(float(x) for x in emb),
    )


def test_empty_store_returns_prompt_unchanged() -> None:
    embedder = DeterministicHashEmbedder(dims=32)
    store = LessonStore()
    out = render_prompt_with_lessons(
        base_prompt="summarize the docs",
        specialty="research-summarize",
        store=store,
        embedder=embedder,
        k=3,
    )
    assert out == "summarize the docs"
    assert "<lessons>" not in out


def test_no_matching_specialty_returns_prompt_unchanged() -> None:
    embedder = DeterministicHashEmbedder(dims=32)
    store = LessonStore()
    store.add(_lesson(specialty="other", task_id="t", text="cite sources", embedder=embedder))
    out = render_prompt_with_lessons(
        base_prompt="summarize the docs",
        specialty="research-summarize",
        store=store,
        embedder=embedder,
        k=3,
    )
    assert "<lessons>" not in out


def test_prepends_top_k_in_lessons_block() -> None:
    embedder = DeterministicHashEmbedder(dims=64)
    store = LessonStore()
    # Five lessons; the three textually closest to the query should win.
    store.add(_lesson(specialty="research-summarize", task_id="t1", text="cite primary sources", embedder=embedder))
    store.add(_lesson(specialty="research-summarize", task_id="t2", text="prefer recent papers", embedder=embedder))
    store.add(_lesson(specialty="research-summarize", task_id="t3", text="quote verbatim sparingly", embedder=embedder))
    store.add(_lesson(specialty="research-summarize", task_id="t4", text="bake bread daily", embedder=embedder))
    store.add(_lesson(specialty="research-summarize", task_id="t5", text="paint walls slowly", embedder=embedder))

    out = render_prompt_with_lessons(
        base_prompt="cite sources from recent papers and quote sparingly",
        specialty="research-summarize",
        store=store,
        embedder=embedder,
        k=3,
    )
    assert out.startswith("<lessons>\n")
    assert out.count("- ") == 3
    assert "cite sources from recent papers and quote sparingly" in out
    # The high-overlap lessons should appear; the irrelevant ones shouldn't.
    assert "cite primary sources" in out
    assert "prefer recent papers" in out
    assert "quote verbatim sparingly" in out
    assert "bake bread daily" not in out
    assert "paint walls slowly" not in out


def test_specialty_filtering_excludes_other_specialties() -> None:
    embedder = DeterministicHashEmbedder(dims=64)
    store = LessonStore()
    store.add(_lesson(specialty="research-summarize", task_id="t1", text="cite sources", embedder=embedder))
    store.add(_lesson(specialty="critic", task_id="t2", text="cite sources", embedder=embedder))

    out = render_prompt_with_lessons(
        base_prompt="cite sources",
        specialty="research-summarize",
        store=store,
        embedder=embedder,
        k=3,
    )
    # Only one lesson available for this specialty.
    assert out.count("- cite sources") == 1


def test_none_store_passes_through() -> None:
    embedder = DeterministicHashEmbedder(dims=32)
    out = render_prompt_with_lessons(
        base_prompt="hi", specialty="x", store=None, embedder=embedder, k=3
    )
    assert out == "hi"


def test_none_embedder_passes_through() -> None:
    store = LessonStore()
    out = render_prompt_with_lessons(
        base_prompt="hi", specialty="x", store=store, embedder=None, k=3
    )
    assert out == "hi"


def test_k_zero_passes_through() -> None:
    embedder = DeterministicHashEmbedder(dims=32)
    store = LessonStore()
    store.add(_lesson(specialty="x", task_id="t", text="lesson", embedder=embedder))
    out = render_prompt_with_lessons(
        base_prompt="hi", specialty="x", store=store, embedder=embedder, k=0
    )
    assert out == "hi"
