"""Unit tests for turing.learning.patterns."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from turing.learning.patterns import PatternExtractor


@dataclass
class FakeLLMResponse:
    content: str = ""
    model: str = "test-model"
    provider: str = "test"
    tokens_used: int = 0
    latency_ms: float = 0.0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _msgs(
    count: int = 5, content: str = "Hello there friend, this is a test message."
) -> list[dict[str, Any]]:
    return [{"role": "user", "content": content} for _ in range(count)]


@pytest.fixture()
def memory_store() -> AsyncMock:
    store = AsyncMock()
    store.get_messages = AsyncMock(return_value=[])
    store.add_fact = AsyncMock()
    store.set_user_preference = AsyncMock()
    return store


@pytest.fixture()
def llm_router() -> AsyncMock:
    router = AsyncMock()
    router.route = AsyncMock(return_value=FakeLLMResponse(content="{}"))
    return router


@pytest.fixture()
def extractor(llm_router: AsyncMock, memory_store: AsyncMock) -> PatternExtractor:
    return PatternExtractor(llm_router, memory_store)


@pytest.mark.asyncio
async def test_extract_no_messages(
    extractor: PatternExtractor, memory_store: AsyncMock, llm_router: AsyncMock
) -> None:
    memory_store.get_messages.return_value = []
    result = await extractor.extract("conv-1")
    assert result == {"facts": [], "preferences": [], "patterns": []}
    llm_router.route.assert_not_awaited()


@pytest.mark.asyncio
async def test_extract_short_conversation_skipped(
    extractor: PatternExtractor, memory_store: AsyncMock, llm_router: AsyncMock
) -> None:
    memory_store.get_messages.return_value = [{"role": "user", "content": "hi"}]
    result = await extractor.extract("conv-2")
    assert result == {"facts": [], "preferences": [], "patterns": []}
    llm_router.route.assert_not_awaited()


@pytest.mark.asyncio
async def test_extract_stores_facts_and_preferences(
    extractor: PatternExtractor, memory_store: AsyncMock, llm_router: AsyncMock
) -> None:
    memory_store.get_messages.return_value = _msgs()
    payload = {
        "facts": [
            {"subject": "Python", "predicate": "is", "object": "a language"},
            {"subject": "", "predicate": "x", "object": "y"},  # skipped
        ],
        "preferences": [
            {"user_id": "u1", "key": "lang", "value": "Python"},
            {"user_id": "u2", "key": "", "value": "x"},  # skipped
        ],
        "patterns": ["uses python", "asks about web"],
    }
    llm_router.route.return_value = FakeLLMResponse(content=json.dumps(payload))

    result = await extractor.extract("conv-3")

    assert len(result["facts"]) == 2
    assert len(result["patterns"]) == 2

    memory_store.add_fact.assert_awaited_once()
    fact_kwargs = memory_store.add_fact.await_args.kwargs
    assert fact_kwargs["subject"] == "Python"
    assert fact_kwargs["predicate"] == "is"
    assert fact_kwargs["obj"] == "a language"
    assert fact_kwargs["source"] == "conversation:conv-3"

    memory_store.set_user_preference.assert_awaited_once_with(
        user_id="u1", key="lang", value="Python"
    )


@pytest.mark.asyncio
async def test_extract_handles_llm_error(
    extractor: PatternExtractor, memory_store: AsyncMock, llm_router: AsyncMock
) -> None:
    memory_store.get_messages.return_value = _msgs()
    llm_router.route.side_effect = RuntimeError("LLM down")

    result = await extractor.extract("conv-4")
    assert result == {"facts": [], "preferences": [], "patterns": []}
    memory_store.add_fact.assert_not_awaited()


@pytest.mark.asyncio
async def test_extract_handles_invalid_json(
    extractor: PatternExtractor, memory_store: AsyncMock, llm_router: AsyncMock
) -> None:
    memory_store.get_messages.return_value = _msgs()
    llm_router.route.return_value = FakeLLMResponse(content="not json at all")

    result = await extractor.extract("conv-5")
    assert result == {"facts": [], "preferences": [], "patterns": []}
    memory_store.add_fact.assert_not_awaited()


@pytest.mark.asyncio
async def test_extract_strips_markdown_code_fence(
    extractor: PatternExtractor, memory_store: AsyncMock, llm_router: AsyncMock
) -> None:
    memory_store.get_messages.return_value = _msgs()
    payload = {
        "facts": [{"subject": "A", "predicate": "B", "object": "C"}],
        "preferences": [],
        "patterns": [],
    }
    fenced = f"```json\n{json.dumps(payload)}\n```"
    llm_router.route.return_value = FakeLLMResponse(content=fenced)

    result = await extractor.extract("conv-6")
    assert len(result["facts"]) == 1
    memory_store.add_fact.assert_awaited_once()


@pytest.mark.asyncio
async def test_extract_skips_messages_without_content(
    extractor: PatternExtractor, memory_store: AsyncMock, llm_router: AsyncMock
) -> None:
    memory_store.get_messages.return_value = [
        {"role": "user", "content": ""},
        {"role": "user", "content": None},
        {
            "role": "user",
            "content": "A long enough piece of content to surpass fifty characters of text.",
        },
    ]
    llm_router.route.return_value = FakeLLMResponse(content="{}")
    result = await extractor.extract("conv-7")
    assert result == {"facts": [], "preferences": [], "patterns": []}
    llm_router.route.assert_awaited_once()


def test_parse_response_non_dict_json(extractor: PatternExtractor) -> None:
    result = extractor._parse_response("[1, 2, 3]")
    assert result == {"facts": [], "preferences": [], "patterns": []}


def test_parse_response_valid_dict(extractor: PatternExtractor) -> None:
    result = extractor._parse_response(
        '{"facts": [{"a": 1}], "preferences": [], "patterns": ["x"]}'
    )
    assert result["facts"] == [{"a": 1}]
    assert result["patterns"] == ["x"]
