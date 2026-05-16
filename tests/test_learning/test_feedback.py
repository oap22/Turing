"""Unit tests for turing.learning.feedback."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from turing.learning.feedback import FeedbackCollector


@pytest.fixture()
def memory_store() -> AsyncMock:
    store = AsyncMock()
    store.log_audit = AsyncMock()
    store.add_fact = AsyncMock()
    return store


@pytest.fixture()
def collector(memory_store: AsyncMock) -> FeedbackCollector:
    return FeedbackCollector(memory_store)


@pytest.mark.asyncio
async def test_record_positive_feedback(
    collector: FeedbackCollector, memory_store: AsyncMock
) -> None:
    await collector.record_feedback(message_id="msg-1", user_id="user-1", positive=True)

    memory_store.log_audit.assert_awaited_once()
    kwargs = memory_store.log_audit.await_args.kwargs
    assert kwargs["action"] == "feedback"
    assert kwargs["user_id"] == "user-1"
    assert kwargs["arguments"] == {"message_id": "msg-1", "feedback": "positive"}
    assert kwargs["result"] == "positive"
    assert kwargs["risk_level"] == "low"
    assert kwargs["approved"] is True


@pytest.mark.asyncio
async def test_record_negative_feedback(
    collector: FeedbackCollector, memory_store: AsyncMock
) -> None:
    await collector.record_feedback(message_id="msg-2", user_id="user-2", positive=False)

    kwargs = memory_store.log_audit.await_args.kwargs
    assert kwargs["arguments"]["feedback"] == "negative"
    assert kwargs["result"] == "negative"


@pytest.mark.asyncio
async def test_record_correction_logs_and_stores_fact(
    collector: FeedbackCollector, memory_store: AsyncMock
) -> None:
    await collector.record_correction(
        user_id="user-3",
        original="The sky is green",
        correction="The sky is blue",
    )

    memory_store.log_audit.assert_awaited_once()
    audit_kwargs = memory_store.log_audit.await_args.kwargs
    assert audit_kwargs["action"] == "correction"
    assert audit_kwargs["user_id"] == "user-3"
    assert audit_kwargs["arguments"]["original"] == "The sky is green"
    assert audit_kwargs["arguments"]["correction"] == "The sky is blue"
    assert audit_kwargs["result"] == "correction_recorded"

    memory_store.add_fact.assert_awaited_once()
    fact_kwargs = memory_store.add_fact.await_args.kwargs
    assert fact_kwargs["subject"] == "correction"
    assert fact_kwargs["predicate"] == "replaces"
    assert "The sky is green" in fact_kwargs["obj"]
    assert "The sky is blue" in fact_kwargs["obj"]
    assert fact_kwargs["confidence"] == 0.9
    assert fact_kwargs["source"] == "user_correction:user-3"


@pytest.mark.asyncio
async def test_record_correction_truncates_long_text(
    collector: FeedbackCollector, memory_store: AsyncMock
) -> None:
    long_original = "x" * 1000
    long_correction = "y" * 1000

    await collector.record_correction(
        user_id="user-4",
        original=long_original,
        correction=long_correction,
    )

    audit_args = memory_store.log_audit.await_args.kwargs["arguments"]
    assert len(audit_args["original"]) == 500
    assert len(audit_args["correction"]) == 500

    fact_obj = memory_store.add_fact.await_args.kwargs["obj"]
    # 200 + " -> " + 200
    assert fact_obj == ("x" * 200) + " -> " + ("y" * 200)
