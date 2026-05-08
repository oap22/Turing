"""Tests for the direct LLM worker (issue #86, AC #1).

`direct_worker` wraps a sync interface around the project's async
`LLMProvider` so the eval harness can call a real model end-to-end. The
provider is dependency-injected so tests run without an API key.
"""

from __future__ import annotations

import asyncio

import pytest

from turing.evals.research_summarize.harness import run
from turing.evals.research_summarize.schema import EvalCase, Expected, SourceDoc
from turing.evals.research_summarize.workers import (
    DirectWorkerConfig,
    direct_worker,
)
from turing.llm.base import LLMResponse, Message, Role


class FakeProvider:
    """Records the last complete() call and returns a canned response."""

    def __init__(self, response: str = "summary text", raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls: list[dict] = []

    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        tools=None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self.raises:
            raise self.raises
        return LLMResponse(content=self.response, model="fake")

    async def stream(self, *_, **__):  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture()
def case() -> EvalCase:
    return EvalCase(
        id="test-1",
        category="claim_preservation",
        prompt="Summarize this for my vault.",
        source_docs=[
            SourceDoc(id="src_1", title="Sleep paper", text="Slow-wave sleep matters."),
            SourceDoc(id="src_2", text="REM does not."),
        ],
        expected=Expected(must_contain_claims=["slow-wave sleep matters"]),
        scoring_fn="score_claim_preservation_v1",
    )


def test_returns_provider_content(case):
    provider = FakeProvider(response="Slow-wave sleep matters for memory consolidation.")
    worker = direct_worker(provider=provider, config=DirectWorkerConfig(model="fake"))
    out = worker(case)
    assert "slow-wave sleep matters" in out.lower()


def test_user_message_includes_prompt_and_source_docs(case):
    provider = FakeProvider()
    worker = direct_worker(provider=provider, config=DirectWorkerConfig(model="fake"))
    worker(case)

    assert len(provider.calls) == 1
    user_messages = [m for m in provider.calls[0]["messages"] if m.role == Role.USER]
    assert user_messages, "no user message sent"
    user_text = user_messages[-1].content

    # Prompt and every source must be in the user message
    assert case.prompt in user_text
    for doc in case.source_docs:
        assert doc.id in user_text
        assert doc.text in user_text


def test_system_prompt_is_research_summarize_specialty(case):
    provider = FakeProvider()
    worker = direct_worker(provider=provider, config=DirectWorkerConfig(model="fake"))
    worker(case)
    system = provider.calls[0]["system"]
    assert "research-summarize" in system.lower() or "summariz" in system.lower()


def test_timeout_surfaces_as_score_zero_via_harness(case):
    """A worker that hangs longer than the configured timeout must not crash
    the harness — the case scores 0 with a timeout note."""

    class HangingProvider:
        async def complete(self, *_, **__):
            await asyncio.sleep(10)
            return LLMResponse(content="never", model="fake")

        async def stream(self, *_, **__):  # pragma: no cover
            raise NotImplementedError

    worker = direct_worker(
        provider=HangingProvider(),
        config=DirectWorkerConfig(model="fake", timeout_s=0.05),
    )

    report = run(worker, [case])
    assert report["per_case"][0]["score"] == 0.0


def test_provider_exception_surfaces_as_score_zero_via_harness(case):
    provider = FakeProvider(raises=RuntimeError("upstream 503"))
    worker = direct_worker(provider=provider, config=DirectWorkerConfig(model="fake"))

    report = run(worker, [case])
    assert report["per_case"][0]["score"] == 0.0
