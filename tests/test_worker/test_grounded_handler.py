"""Tests for GroundedResearchHandler — the worker's RESEARCH-kind wire (#261).

The handler runs the grounded Think→Act loop for a dispatched subtask and packs
the reasoning trajectory into TaskResult.fragment so the coordinator's episode
is reasoning-bearing. Driven with a scripted LLM + stub fetcher and a tmp vault.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from turing.coordinator.dispatch import (
    SubtaskDispatch,
    SubtaskKind,
    reasoning_from_result,
)
from turing.coordinator.dispatch.grounding_fragment import (
    GROUNDING_FRAGMENT_KEY,
    grounding_to_fragment,
    merge_fragments,
)
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.llm.base import LLMResponse, ToolCall
from turing.vault.inbox_writer import InboxDraftWriter
from turing.worker.grounded_handler import GroundedResearchHandler
from turing.worker.grounding import GroundedResearcher
from turing.worker.tools.web_fetch import WebFetchAllowlist

if TYPE_CHECKING:
    from pathlib import Path

SPECIALTY = "ai-ml-generalist"


class ScriptedLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self._i = 0

    async def complete(self, messages, system="", tools=None, max_tokens=4096, temperature=0.7):
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


async def _fake_fetcher(url: str) -> tuple[str, str]:
    return (f"Title for {url}", f"Real source text from {url} explaining QLoRA.")


def _fetch_then_answer() -> list[LLMResponse]:
    return [
        LLMResponse(
            content="I should ground this in a real paper.",
            tool_calls=[
                ToolCall(
                    id="t1", name="web_fetch", arguments={"url": "https://arxiv.org/abs/2305.14314"}
                )
            ],
            model="qwen2.5:7b",
            usage={"total_tokens": 50},
        ),
        LLMResponse(
            content="QLoRA fine-tunes a quantised base with low-rank adapters.",
            model="qwen2.5:7b",
            usage={"total_tokens": 40},
        ),
    ]


def _researcher(tmp_path: Path, responses: list[LLMResponse]) -> GroundedResearcher:
    return GroundedResearcher(
        llm=ScriptedLLM(responses),
        allowlist=WebFetchAllowlist(("arxiv.org",)),
        fetcher=_fake_fetcher,
        writer=InboxDraftWriter(vault_root=tmp_path),
        worker_id="jetson-1",
        now_ms=1000,
    )


def _handler(researcher: GroundedResearcher) -> GroundedResearchHandler:
    clock = {"t": 5000}

    def now() -> int:
        clock["t"] += 1
        return clock["t"]

    return GroundedResearchHandler(researcher=researcher, worker_id="jetson-1", now_ms=now)


def _envelope() -> SubtaskDispatch:
    return SubtaskDispatch(
        subtask_id="q-qlora",
        task_id="night-1",
        specialty=SPECIALTY,
        prompt="Explain QLoRA.",
        source_inputs=[],
        deadline_ms=10_000,
        kind=SubtaskKind.RESEARCH.value,
    )


@pytest.mark.asyncio
async def test_handler_returns_completed_result_with_reasoning_fragment(tmp_path: Path) -> None:
    handler = _handler(_researcher(tmp_path, _fetch_then_answer()))

    result = await handler.handle(_envelope())

    assert result.status == SubtaskState.COMPLETED.value
    assert result.subtask_id == "q-qlora"
    assert result.worker_id == "jetson-1"
    assert "QLoRA" in result.output
    # The reasoning trajectory rides home in the fragment.
    reasoning = reasoning_from_result(result)
    assert reasoning
    assert any("ground this" in step for step in reasoning)


@pytest.mark.asyncio
async def test_handler_writes_inbox_draft_keyed_by_subtask(tmp_path: Path) -> None:
    handler = _handler(_researcher(tmp_path, _fetch_then_answer()))

    await handler.handle(_envelope())

    draft = tmp_path / "vault" / "inbox" / "q-qlora" / "answer.md"
    assert draft.exists()
    text = draft.read_text(encoding="utf-8")
    assert "## Sources" in text
    assert "arxiv.org" in text


@pytest.mark.asyncio
async def test_handler_reports_failure_without_crashing(tmp_path: Path) -> None:
    class _Boom(GroundedResearcher):
        async def research(self, **_kw):  # type: ignore[override]
            raise RuntimeError("fetcher exploded")

    boom = _Boom(
        llm=ScriptedLLM(_fetch_then_answer()),
        allowlist=WebFetchAllowlist(("arxiv.org",)),
        fetcher=_fake_fetcher,
        writer=InboxDraftWriter(vault_root=tmp_path),
        worker_id="jetson-1",
        now_ms=1000,
    )

    result = await _handler(boom).handle(_envelope())

    assert result.status == SubtaskState.FAILED.value
    assert result.error == "fetcher exploded"
    assert reasoning_from_result(result) == ()


# ── grounding_fragment helpers ───────────────────────────────────────────────


def test_grounding_fragment_roundtrips_reasoning() -> None:
    from turing.coordinator.dispatch import TaskResult

    frag = grounding_to_fragment(reasoning=["step a", "step b"], confidence=0.7, source_count=2)
    assert frag[GROUNDING_FRAGMENT_KEY]["reasoning"] == ["step a", "step b"]

    result = TaskResult(
        subtask_id="s",
        worker_id="w",
        status="COMPLETED",
        output="o",
        tokens_used=1,
        latency_ms=1,
        model="m",
        fragment=frag,
    )
    assert reasoning_from_result(result) == ("step a", "step b")


def test_reasoning_from_result_tolerates_missing_or_malformed() -> None:
    from turing.coordinator.dispatch import TaskResult

    def _result(fragment) -> TaskResult:
        return TaskResult(
            subtask_id="s",
            worker_id="w",
            status="COMPLETED",
            output="",
            tokens_used=0,
            latency_ms=0,
            model="",
            fragment=fragment,
        )

    assert reasoning_from_result(_result(None)) == ()
    assert reasoning_from_result(_result({})) == ()
    assert reasoning_from_result(_result({GROUNDING_FRAGMENT_KEY: "nope"})) == ()
    assert reasoning_from_result(_result({GROUNDING_FRAGMENT_KEY: {"reasoning": "x"}})) == ()


def test_merge_fragments_combines_and_drops_empty() -> None:
    grounding = grounding_to_fragment(reasoning=["r"])
    proposals = {"proposed_questions": [{"prompt": "next?", "specialty": SPECIALTY}]}

    merged = merge_fragments(grounding, proposals)
    assert merged is not None
    assert GROUNDING_FRAGMENT_KEY in merged
    assert "proposed_questions" in merged

    assert merge_fragments(None, {}) is None
