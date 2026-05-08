"""LLM-judge scorer: claim_recall against expected_claims, mocked LLM."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from turing.learning.eval_set import claim_recall
from turing.llm.base import LLMResponse


def _llm(content: str) -> AsyncMock:
    """A fake LLM router returning a canned response — matches conftest's
    mock_llm_response fixture pattern."""
    fake = AsyncMock()
    fake.complete = AsyncMock(return_value=LLMResponse(content=content, model="judge"))
    return fake


@pytest.mark.asyncio
async def test_judge_says_all_claims_present_returns_one() -> None:
    judge = _llm('{"recalled": ["a", "b"], "missing": []}')
    score = await claim_recall(
        output="any output",
        expected_claims=["a", "b"],
        llm=judge,
    )
    assert score == 1.0


@pytest.mark.asyncio
async def test_judge_says_half_present_returns_half() -> None:
    judge = _llm('{"recalled": ["a"], "missing": ["b"]}')
    score = await claim_recall(
        output="x",
        expected_claims=["a", "b"],
        llm=judge,
    )
    assert score == 0.5


@pytest.mark.asyncio
async def test_empty_expected_returns_one() -> None:
    """Nothing to recall is a vacuous pass — and we never call the LLM."""
    judge = _llm('this should never be reached')
    score = await claim_recall(output="x", expected_claims=[], llm=judge)
    assert score == 1.0
    judge.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_emits_garbage_returns_zero_safely() -> None:
    """A malformed LLM response must not crash the harness — return 0.0
    so the case fails the gate loudly instead of skipping it."""
    judge = _llm("not json at all")
    score = await claim_recall(
        output="x",
        expected_claims=["a"],
        llm=judge,
    )
    assert score == 0.0


@pytest.mark.asyncio
async def test_judge_never_writes_expected_claims() -> None:
    """The hard rule: never let an LLM generate ``expected_*`` content. We
    enforce it by structurally — claim_recall asks the LLM only to label
    presence/absence over already-given claims. The system prompt below
    must *forbid* the LLM from inventing new claims."""
    from turing.learning.eval_set.judge import _SYSTEM_PROMPT

    lower = _SYSTEM_PROMPT.lower()
    assert "do not invent" in lower or "never invent" in lower or "do not synthesise" in lower or "do not generate" in lower


@pytest.mark.asyncio
async def test_mutation_drop_a_claim_drops_score() -> None:
    """Mutation: with a known-good output the judge returns recall=1.0;
    the same output minus one claim must drop below 1.0."""
    full_judge = _llm('{"recalled": ["a", "b"], "missing": []}')
    full_score = await claim_recall(
        output="full", expected_claims=["a", "b"], llm=full_judge
    )

    dropped_judge = _llm('{"recalled": ["a"], "missing": ["b"]}')
    dropped_score = await claim_recall(
        output="missing one", expected_claims=["a", "b"], llm=dropped_judge
    )
    assert full_score == 1.0
    assert dropped_score < 1.0
