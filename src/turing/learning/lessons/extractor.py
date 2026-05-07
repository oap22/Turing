"""LessonExtractor — turn a closed Episode into a Lesson via an LLM call.

The extraction prompt is versioned alongside the schema. Storing the version
tag inside the system prompt makes prompt drift observable (a CI test asserts
the tag appears verbatim) and makes it explicit that lessons emitted by an
older extractor live alongside lessons from a newer one until an offline
re-extraction migrates them.
"""

from __future__ import annotations

from typing import Callable, Protocol

from turing.coordinator.lifecycle.episode_store import Episode
from turing.learning.lessons.lesson import (
    CURRENT_LESSON_SCHEMA_VERSION,
    Lesson,
)
from turing.llm.base import Message, Role


_SYSTEM_PROMPT_TEMPLATE = (
    "You are the cluster's lessons extractor (schema v{version}). "
    "Read the episode below and produce ONE short, generalisable lesson "
    "specific to the worker's specialty: what worked, what didn't. "
    "Reply with the lesson text only — no preamble, no JSON wrapper."
)


class _CompletableLLM(Protocol):
    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        **_: object,
    ) -> object: ...


class LessonExtractor:
    def __init__(
        self,
        *,
        llm_router: _CompletableLLM,
        embed: Callable[[str], list[float] | tuple[float, ...]],
    ) -> None:
        self._llm = llm_router
        self._embed = embed

    async def extract(self, episode: Episode) -> Lesson:
        system = _SYSTEM_PROMPT_TEMPLATE.format(
            version=CURRENT_LESSON_SCHEMA_VERSION
        )
        user_text = (
            f"specialty: {episode.specialty}\n"
            f"input: {episode.input_text}\n"
            f"output: {episode.output_text}\n"
            f"trajectory: {' | '.join(episode.trajectory)}\n"
            f"success: {episode.success}\n"
            f"critic_score: {episode.critic_score}\n"
        )
        response = await self._llm.complete(
            messages=[Message(role=Role.USER, content=user_text)],
            system=system,
        )
        text = getattr(response, "content", "").strip()
        embedding = tuple(float(x) for x in self._embed(text))
        return Lesson(
            specialty=episode.specialty,
            task_id=episode.task_id,
            text=text,
            embedding=embedding,
        )
