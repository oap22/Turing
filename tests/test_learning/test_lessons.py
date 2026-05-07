"""Tests for the lessons-extractor + auto-injection pipeline (#19).

Three deliverables exercised here:

1. ``Lesson`` model + versioned schema
2. ``LessonExtractor`` produces a structured lesson from an episode via an
   LLM stub
3. ``LessonStore`` indexes lessons and returns top-k by cosine similarity
4. ``inject_lessons_into_prompt`` formats top-k lessons under a labelled
   ``<lessons>`` section
5. A/B fixture: a worker prompt with retrieved lessons differs from one
   without — a measurable downstream signal
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.lifecycle.episode_store import Episode
from turing.learning.lessons import (
    CURRENT_LESSON_SCHEMA_VERSION,
    Lesson,
    LessonExtractor,
    LessonStore,
    LessonSchemaError,
    inject_lessons_into_prompt,
)


def _episode(specialty: str = "research-summarize") -> Episode:
    return Episode(
        task_id="t-1",
        subtask_id="t-1.s-0",
        worker_id="pi-beta",
        specialty=specialty,
        model_version="qwen2.5-7b@1.0",
        adapter_version="research-summarize@1.2",
        input_text="summarise paper",
        trajectory=("vault_query: paper", "draft", "polish"),
        output_text="summary text",
        success=True,
        latency_ms=1234,
        tokens_used=512,
        outcome=SubtaskState.COMPLETED,
        critic_score=0.85,
        recorded_at_ms=1700_000_000_000,
    )


# ── Lesson model ─────────────────────────────────────────────────────


class TestLesson:
    def test_round_trip_to_dict(self) -> None:
        lesson = Lesson(
            specialty="research-summarize",
            task_id="t-1",
            text="cite primary sources before secondary",
            embedding=(0.1, 0.2, 0.3),
        )
        assert lesson.schema_version == CURRENT_LESSON_SCHEMA_VERSION
        d = lesson.to_dict()
        assert d["schema_version"] == CURRENT_LESSON_SCHEMA_VERSION
        assert d["specialty"] == "research-summarize"
        again = Lesson.from_dict(d)
        assert again == lesson

    def test_old_schema_version_rejected(self) -> None:
        bad = {
            "schema_version": 0,
            "specialty": "x",
            "task_id": "t",
            "text": "y",
            "embedding": [0.0],
        }
        with pytest.raises(LessonSchemaError):
            Lesson.from_dict(bad)


# ── extractor ─────────────────────────────────────────────────────────


class TestLessonExtractor:
    @pytest.mark.asyncio
    async def test_extract_returns_lesson(self) -> None:
        llm = AsyncMock()

        from turing.llm.base import LLMResponse

        llm.complete = AsyncMock(
            return_value=LLMResponse(
                content="Lessons: cite primary sources first.",
                model="claude",
            )
        )

        def fake_embed(text: str) -> list[float]:
            return [0.1, 0.2, 0.3]

        extractor = LessonExtractor(llm_router=llm, embed=fake_embed)
        lesson = await extractor.extract(_episode())
        assert isinstance(lesson, Lesson)
        assert lesson.specialty == "research-summarize"
        assert lesson.task_id == "t-1"
        assert lesson.text  # non-empty

    @pytest.mark.asyncio
    async def test_extract_uses_versioned_prompt(self) -> None:
        """The extraction prompt is versioned alongside the schema."""
        llm = AsyncMock()
        from turing.llm.base import LLMResponse

        llm.complete = AsyncMock(
            return_value=LLMResponse(content="lessons body", model="claude")
        )

        extractor = LessonExtractor(llm_router=llm, embed=lambda _t: [0.0])
        await extractor.extract(_episode())

        call = llm.complete.call_args
        system = call.kwargs.get("system") or call[1].get("system", "")
        # The prompt template is versioned so prompt drift is observable in
        # CI; the version tag must appear in the system prompt.
        assert f"v{CURRENT_LESSON_SCHEMA_VERSION}" in system


# ── store / top-k retrieval ───────────────────────────────────────────


class TestLessonStore:
    def test_query_returns_top_k_by_cosine(self) -> None:
        store = LessonStore()
        store.add(
            Lesson(
                specialty="research-summarize",
                task_id="t-a",
                text="A",
                embedding=(1.0, 0.0),
            )
        )
        store.add(
            Lesson(
                specialty="research-summarize",
                task_id="t-b",
                text="B",
                embedding=(0.0, 1.0),
            )
        )
        results = store.query(
            specialty="research-summarize", embedding=(1.0, 0.0), k=1
        )
        assert len(results) == 1
        assert results[0].text == "A"

    def test_query_filters_by_specialty(self) -> None:
        store = LessonStore()
        store.add(
            Lesson(
                specialty="research-summarize",
                task_id="t-a",
                text="A",
                embedding=(1.0, 0.0),
            )
        )
        store.add(
            Lesson(
                specialty="code-debug",
                task_id="t-b",
                text="B",
                embedding=(1.0, 0.0),
            )
        )
        results = store.query(
            specialty="research-summarize", embedding=(1.0, 0.0), k=5
        )
        assert len(results) == 1
        assert results[0].specialty == "research-summarize"

    def test_query_handles_empty_store(self) -> None:
        store = LessonStore()
        results = store.query(specialty="x", embedding=(0.0, 0.0), k=3)
        assert results == []

    def test_orders_by_descending_similarity(self) -> None:
        store = LessonStore()
        store.add(
            Lesson(
                specialty="x",
                task_id="t1",
                text="far",
                embedding=(0.1, 0.99),
            )
        )
        store.add(
            Lesson(
                specialty="x",
                task_id="t2",
                text="near",
                embedding=(1.0, 0.0),
            )
        )
        results = store.query(specialty="x", embedding=(1.0, 0.0), k=2)
        assert [r.text for r in results] == ["near", "far"]


# ── prompt injection ──────────────────────────────────────────────────


class TestPromptInjector:
    def test_renders_lessons_block(self) -> None:
        lessons = [
            Lesson(
                specialty="x",
                task_id="t1",
                text="cite primary sources",
                embedding=(1.0,),
            ),
            Lesson(
                specialty="x",
                task_id="t2",
                text="prefer concise summaries",
                embedding=(1.0,),
            ),
        ]
        prompt = inject_lessons_into_prompt(
            base_prompt="You are a worker.", lessons=lessons
        )
        assert "<lessons>" in prompt
        assert "</lessons>" in prompt
        assert "cite primary sources" in prompt
        assert "prefer concise summaries" in prompt

    def test_no_lessons_leaves_prompt_unchanged(self) -> None:
        prompt = inject_lessons_into_prompt(
            base_prompt="You are a worker.", lessons=[]
        )
        assert "<lessons>" not in prompt
        assert prompt == "You are a worker."

    def test_lessons_block_appears_after_base_prompt(self) -> None:
        lessons = [
            Lesson(
                specialty="x",
                task_id="t1",
                text="L1",
                embedding=(1.0,),
            )
        ]
        prompt = inject_lessons_into_prompt(
            base_prompt="BASE", lessons=lessons
        )
        assert prompt.startswith("BASE")
        assert prompt.index("<lessons>") > prompt.index("BASE")


# ── A/B fixture ───────────────────────────────────────────────────────


class TestAbFixture:
    """When matching lessons exist, the worker sees a different prompt.

    This is the smallest empirical demonstration that retrieved lessons
    affect the downstream prompt — exactly the signal the acceptance
    criterion asks for. A real LLM-comparison eval lives outside the unit
    suite (slice 21 / eval set authoring).
    """

    def test_prompt_with_lessons_differs_from_prompt_without(self) -> None:
        store = LessonStore()
        store.add(
            Lesson(
                specialty="research-summarize",
                task_id="t-a",
                text="cite primary sources",
                embedding=(1.0, 0.0),
            )
        )

        def build_prompt(*, with_lessons: bool) -> str:
            base = "Summarise the paper."
            if not with_lessons:
                return base
            top_k = store.query(
                specialty="research-summarize",
                embedding=(1.0, 0.0),
                k=3,
            )
            return inject_lessons_into_prompt(base_prompt=base, lessons=top_k)

        with_lessons = build_prompt(with_lessons=True)
        without_lessons = build_prompt(with_lessons=False)
        assert with_lessons != without_lessons
        assert "cite primary sources" in with_lessons
        assert "cite primary sources" not in without_lessons
