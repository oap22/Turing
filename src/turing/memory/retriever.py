"""Memory retriever — assembles context from multiple memory sources in parallel."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from turing.telemetry import traced

if TYPE_CHECKING:
    from turing.memory.embeddings import EmbeddingModel
    from turing.memory.store import MemoryStore
    from turing.memory.vectors import VectorStore

logger = structlog.get_logger(__name__)


@dataclass
class RetrievalResult:
    """Aggregated retrieval output from all memory sources."""

    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    relevant_memories: list[dict[str, Any]] = field(default_factory=list)
    user_preferences: dict[str, str] = field(default_factory=dict)
    relevant_facts: list[dict[str, Any]] = field(default_factory=list)


class MemoryRetriever:
    """Retrieves relevant context from memory for a given user message.

    All four memory sources are queried concurrently via
    :func:`asyncio.gather` to minimise latency.
    """

    def __init__(
        self,
        store: MemoryStore,
        vector_store: VectorStore,
        embedding_model: EmbeddingModel,
    ) -> None:
        self._store = store
        self._vectors = vector_store
        self._embeddings = embedding_model

    @traced("memory.retrieve")
    async def retrieve(
        self,
        message: str,
        channel_id: str,
        user_id: str,
        limit: int = 10,
    ) -> RetrievalResult:
        """Retrieve context from all memory sources in parallel.

        Parameters
        ----------
        message:
            The current user message used for semantic search.
        channel_id:
            Channel to scope recent-message retrieval.
        user_id:
            User whose preferences should be loaded.
        limit:
            Maximum number of items to return from each source.
        """
        recent_task = self._fetch_recent_messages(channel_id, limit)
        semantic_task = self._fetch_semantic_matches(message, limit)
        prefs_task = self._fetch_user_preferences(user_id)
        facts_task = self._fetch_relevant_facts(message, limit)

        results = await asyncio.gather(
            recent_task,
            semantic_task,
            prefs_task,
            facts_task,
            return_exceptions=True,
        )

        recent_messages = _unwrap(results[0], [])
        relevant_memories = _unwrap(results[1], [])
        user_preferences = _unwrap(results[2], {})
        relevant_facts = _unwrap(results[3], [])

        logger.debug(
            "memory_retrieval_complete",
            recent_count=len(recent_messages),
            semantic_count=len(relevant_memories),
            pref_count=len(user_preferences),
            fact_count=len(relevant_facts),
        )

        return RetrievalResult(
            recent_messages=recent_messages,
            relevant_memories=relevant_memories,
            user_preferences=user_preferences,
            relevant_facts=relevant_facts,
        )

    # ── individual fetchers ───────────────────────────────────────────

    async def _fetch_recent_messages(self, channel_id: str, limit: int) -> list[dict[str, Any]]:
        """Fetch the most recent messages in the channel."""
        return await self._store.get_recent_messages(channel_id, limit=limit)

    async def _fetch_semantic_matches(self, message: str, limit: int) -> list[dict[str, Any]]:
        """Embed the message and find similar stored memories."""
        if not self._embeddings.ready:
            return []

        query_vec = await self._embeddings.embed(message)

        # A zero-vector means the embedding model returned a fallback.
        if all(v == 0.0 for v in query_vec):
            return []

        matches = await self._vectors.search(query_vec, limit=limit)
        if not matches:
            return []

        # One batched fetch rather than a lookup per hit. `matches` is already
        # ordered nearest-first, so we re-apply that order to the mapping
        # instead of taking whatever order the database returned rows in.
        by_id = await self._store.get_messages_by_ids([mid for mid, _ in matches])

        memories: list[dict[str, Any]] = []
        for memory_id, distance in matches:
            row = by_id.get(memory_id)
            if row is not None:
                # Copy per hit: the batch returns one dict per id, so two hits
                # on the same id would otherwise share it and the second
                # `distance` would overwrite the first.
                memories.append({**row, "distance": distance})
        return memories

    async def _fetch_user_preferences(self, user_id: str) -> dict[str, str]:
        """Load all stored preferences for the user."""
        return await self._store.get_user_preferences(user_id)

    async def _fetch_relevant_facts(self, message: str, limit: int) -> list[dict[str, Any]]:
        """Text-search the fact store for entries matching the message."""
        # Use the first few significant words as the search query
        words = message.split()
        # Skip very short messages (greetings etc.) — not useful for fact search
        if len(words) < 2:
            return []
        query = " ".join(words[:8])  # first 8 words as search seed
        return await self._store.search_facts(query, limit=limit)


def _unwrap(result: Any, default: Any) -> Any:
    """Return *result* if it is not an exception, else log and return *default*."""
    if isinstance(result, BaseException):
        logger.warning("memory_retrieval_source_failed", error=str(result))
        return default
    return result
