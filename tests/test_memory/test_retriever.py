"""Tests for the MemoryRetriever."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turing.memory.embeddings import EmbeddingModel
from turing.memory.retriever import MemoryRetriever, RetrievalResult
from turing.memory.store import MemoryStore
from turing.memory.vectors import VectorStore


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_store() -> AsyncMock:
    store = AsyncMock(spec=MemoryStore)
    store.get_recent_messages = AsyncMock(
        return_value=[
            {"id": 1, "role": "user", "content": "hello", "timestamp": "2024-01-01T00:00:00Z"},
            {"id": 2, "role": "assistant", "content": "hi!", "timestamp": "2024-01-01T00:00:01Z"},
        ]
    )
    store.get_user_preferences = AsyncMock(
        return_value={"theme": "dark", "language": "en"}
    )
    store.search_facts = AsyncMock(
        return_value=[
            {
                "id": 1,
                "subject": "Pi",
                "predicate": "has_model",
                "object": "4B",
                "confidence": 1.0,
            }
        ]
    )
    store.get_message_by_id = AsyncMock(
        return_value={
            "id": 10,
            "role": "user",
            "content": "relevant message",
            "timestamp": "2024-01-01T00:00:00Z",
        }
    )
    return store


@pytest.fixture
def mock_vector_store() -> AsyncMock:
    vs = AsyncMock(spec=VectorStore)
    vs.search = AsyncMock(return_value=[(10, 0.25)])
    return vs


@pytest.fixture
def mock_embedding_model() -> MagicMock:
    em = MagicMock(spec=EmbeddingModel)
    em.ready = True
    em.embed = AsyncMock(return_value=[0.1] * 384)
    return em


@pytest.fixture
def mock_embedding_model_unavailable() -> MagicMock:
    em = MagicMock(spec=EmbeddingModel)
    em.ready = False
    em.embed = AsyncMock(return_value=[0.0] * 384)
    return em


# ── retrieval tests ──────────────────────────────────────────────────


class TestRetriever:
    @pytest.mark.asyncio
    async def test_full_retrieval(
        self,
        mock_store: AsyncMock,
        mock_vector_store: AsyncMock,
        mock_embedding_model: MagicMock,
    ) -> None:
        retriever = MemoryRetriever(mock_store, mock_vector_store, mock_embedding_model)
        result = await retriever.retrieve(
            message="What is the Pi model?",
            channel_id="general",
            user_id="u1",
            limit=5,
        )

        assert isinstance(result, RetrievalResult)
        assert len(result.recent_messages) == 2
        assert len(result.relevant_memories) == 1
        assert result.relevant_memories[0]["content"] == "relevant message"
        assert result.user_preferences == {"theme": "dark", "language": "en"}
        assert len(result.relevant_facts) == 1

    @pytest.mark.asyncio
    async def test_parallel_execution(
        self,
        mock_store: AsyncMock,
        mock_vector_store: AsyncMock,
        mock_embedding_model: MagicMock,
    ) -> None:
        """All four retrieval sources should be called."""
        retriever = MemoryRetriever(mock_store, mock_vector_store, mock_embedding_model)
        await retriever.retrieve(
            message="How is the system?",
            channel_id="ch1",
            user_id="u1",
        )

        mock_store.get_recent_messages.assert_awaited_once()
        mock_embedding_model.embed.assert_awaited_once()
        mock_vector_store.search.assert_awaited_once()
        mock_store.get_user_preferences.assert_awaited_once()
        mock_store.search_facts.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_embedding_unavailable_skips_semantic(
        self,
        mock_store: AsyncMock,
        mock_vector_store: AsyncMock,
        mock_embedding_model_unavailable: MagicMock,
    ) -> None:
        retriever = MemoryRetriever(
            mock_store, mock_vector_store, mock_embedding_model_unavailable
        )
        result = await retriever.retrieve(
            message="test query",
            channel_id="ch1",
            user_id="u1",
        )
        assert result.relevant_memories == []
        mock_vector_store.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_short_message_skips_fact_search(
        self,
        mock_store: AsyncMock,
        mock_vector_store: AsyncMock,
        mock_embedding_model: MagicMock,
    ) -> None:
        """Single-word messages should skip fact search."""
        retriever = MemoryRetriever(mock_store, mock_vector_store, mock_embedding_model)
        result = await retriever.retrieve(
            message="hi",
            channel_id="ch1",
            user_id="u1",
        )
        mock_store.search_facts.assert_not_awaited()
        assert result.relevant_facts == []


class TestRetrievalResultDefaults:
    def test_defaults(self) -> None:
        result = RetrievalResult()
        assert result.recent_messages == []
        assert result.relevant_memories == []
        assert result.user_preferences == {}
        assert result.relevant_facts == []


class TestRetrieverErrorHandling:
    @pytest.mark.asyncio
    async def test_store_failure_returns_defaults(
        self,
        mock_vector_store: AsyncMock,
        mock_embedding_model: MagicMock,
    ) -> None:
        """If one source raises, the others should still succeed."""
        store = AsyncMock(spec=MemoryStore)
        store.get_recent_messages = AsyncMock(side_effect=RuntimeError("db error"))
        store.get_user_preferences = AsyncMock(return_value={"key": "val"})
        store.search_facts = AsyncMock(return_value=[])
        store.get_message_by_id = AsyncMock(
            return_value={"id": 1, "content": "x", "role": "user", "timestamp": "t"}
        )

        retriever = MemoryRetriever(store, mock_vector_store, mock_embedding_model)
        result = await retriever.retrieve(
            message="some longer query here",
            channel_id="ch1",
            user_id="u1",
        )
        # recent_messages should have the default (empty list) due to error
        assert result.recent_messages == []
        # user_preferences should still work
        assert result.user_preferences == {"key": "val"}

    @pytest.mark.asyncio
    async def test_vector_search_failure_returns_empty(
        self,
        mock_store: AsyncMock,
        mock_embedding_model: MagicMock,
    ) -> None:
        vs = AsyncMock(spec=VectorStore)
        vs.search = AsyncMock(side_effect=RuntimeError("vec error"))

        retriever = MemoryRetriever(mock_store, vs, mock_embedding_model)
        result = await retriever.retrieve(
            message="longer query for testing",
            channel_id="ch1",
            user_id="u1",
        )
        assert result.relevant_memories == []
        # Other sources should still work
        assert len(result.recent_messages) == 2
