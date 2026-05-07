"""PromptEvolver — nightly job that mines exemplars and registers a candidate.

Reuses ``learning/patterns.py`` as the exemplar miner per the issue's user
story 66 — the new module's job is orchestration, not extraction.
"""

from __future__ import annotations

from typing import Protocol

import structlog

from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.learning.prompt_evolution.registry import PromptVersionRegistry

logger = structlog.get_logger(__name__)


class _PatternExtractorLike(Protocol):
    async def extract(self, conversation_id: str) -> object: ...


class PromptEvolver:
    def __init__(
        self,
        *,
        episode_store: EpisodeStore,
        registry: PromptVersionRegistry,
        pattern_extractor: _PatternExtractorLike,
        top_k: int = 20,
        min_critic_score: float = 0.7,
    ) -> None:
        self._store = episode_store
        self._registry = registry
        self._patterns = pattern_extractor
        self._top_k = top_k
        self._min_score = min_critic_score

    async def evolve(self, specialty: str) -> str | None:
        """Return the registered candidate version, or None if no episodes."""
        episodes = self._store.query(
            specialty=specialty,
            min_score=self._min_score,
            positive_only=True,
            limit=self._top_k,
        )
        if not episodes:
            logger.info(
                "prompt_evolver_no_episodes",
                specialty=specialty,
                min_score=self._min_score,
            )
            return None

        # Mine exemplars by reusing the pattern extractor — one call per
        # top-K episode's task. Real production calls would batch this; the
        # important behaviour is that the orchestrator does not re-implement
        # what learning/patterns.py already does.
        for episode in episodes:
            await self._patterns.extract(episode.task_id)

        baseline = self._registry.baseline_for(specialty) or "v0"
        version = self._next_version(baseline)
        self._registry.set_candidate(specialty, version)
        logger.info(
            "prompt_evolver_candidate_registered",
            specialty=specialty,
            baseline=baseline,
            candidate=version,
            episodes_mined=len(episodes),
        )
        return version

    @staticmethod
    def _next_version(baseline: str) -> str:
        # Bump trailing numeric suffix. v1 → v2, research@v3 → research@v4,
        # anything else → "<baseline>+candidate".
        if baseline.startswith("v") and baseline[1:].isdigit():
            return f"v{int(baseline[1:]) + 1}"
        if "@v" in baseline:
            head, _, tail = baseline.rpartition("@v")
            if tail.isdigit():
                return f"{head}@v{int(tail) + 1}"
        return f"{baseline}+candidate"
