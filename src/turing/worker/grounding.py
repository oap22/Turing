"""Grounded research executor (ADR 0009 §2 "Night", issue #262).

A worker answers a dispatched question by running the Think→Act loop while
**grounding its answer in fetched external sources** via the allowlisted
``web_fetch`` tool — the load-bearing "knowledge enters from outside the student
model" rule. The result:

- a **draft** written to ``vault/inbox/<task_id>/<slug>.md`` with the standard
  frontmatter **plus the fetched source list** (provenance for morning review);
- an **episode** that captures the **reasoning trajectory**, not just the final
  answer — the reasoning is the SFT target the dataset builder later needs.

The LLM and the network fetcher are both injected, so the loop is fully
testable with a canned-response provider and a stub fetcher (no GPU, no live
HTTP), per the project's test conventions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.untrusted_wrap import wrap_untrusted
from turing.llm.base import LLMResponse, Message, Role, ToolDefinition
from turing.worker.tools.web_fetch import (
    FetchedSource,
    WebFetchAllowlist,
    WebFetchNotAllowedError,
    web_fetch,
)

if TYPE_CHECKING:
    from pathlib import Path

    from turing.vault.inbox_writer import InboxDraftWriter
    from turing.worker.tools.web_fetch import Fetcher

WEB_FETCH_TOOL = ToolDefinition(
    name="web_fetch",
    description=(
        "Fetch an allowlisted external URL and return its text so you can ground "
        "your answer in real sources. Call this before answering; cite what you "
        "fetched. Returns an error if the URL's host is not on the allowlist."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch (must be allowlisted)."}
        },
        "required": ["url"],
    },
)

_SYSTEM_PROMPT = (
    "You are an AI/ML research worker. Answer the question by FIRST fetching "
    "real external sources with the web_fetch tool, then synthesising a grounded, "
    "reasoning-bearing answer that cites them. Do not answer from memory alone — "
    "knowledge must come from the fetched sources. Show your reasoning."
)


class GroundingLLM(Protocol):
    """The slice of the LLM provider interface the grounding loop needs."""

    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse: ...


@dataclass(frozen=True)
class GroundedDraft:
    """The outcome of a grounded research run."""

    task_id: str
    specialty: str
    answer: str
    reasoning: tuple[str, ...]
    sources: tuple[FetchedSource, ...]
    confidence: float
    tokens_used: int
    model: str
    draft_path: Path

    @property
    def is_grounded(self) -> bool:
        return bool(self.sources)


class GroundedResearcher:
    """Runs the grounded Think→Act loop and writes the inbox draft."""

    def __init__(
        self,
        *,
        llm: GroundingLLM,
        allowlist: WebFetchAllowlist,
        fetcher: Fetcher,
        writer: InboxDraftWriter,
        worker_id: str,
        now_ms: int,
        max_iterations: int = 6,
    ) -> None:
        self._llm = llm
        self._allowlist = allowlist
        self._fetcher = fetcher
        self._writer = writer
        self._worker_id = worker_id
        self._now_ms = now_ms
        self._max_iterations = max_iterations

    async def research(
        self,
        *,
        task_id: str,
        prompt: str,
        specialty: str,
        slug: str = "answer",
    ) -> GroundedDraft:
        messages: list[Message] = [Message(role=Role.USER, content=prompt)]
        reasoning: list[str] = []
        sources: list[FetchedSource] = []
        tokens = 0
        model = ""
        answer = ""

        for _ in range(self._max_iterations):
            resp = await self._llm.complete(
                messages, system=_SYSTEM_PROMPT, tools=[WEB_FETCH_TOOL]
            )
            model = resp.model or model
            tokens += int(resp.usage.get("total_tokens", 0))
            if resp.content:
                reasoning.append(resp.content)

            if not resp.tool_calls:
                answer = resp.content
                break

            messages.append(
                Message(role=Role.ASSISTANT, content=resp.content, tool_calls=resp.tool_calls)
            )
            for call in resp.tool_calls:
                tool_output = await self._run_tool_call(call, sources)
                messages.append(
                    Message(role=Role.TOOL, content=tool_output, tool_call_id=call.id)
                )
        else:
            # Loop exhausted without a final text turn — use the last reasoning.
            answer = reasoning[-1] if reasoning else ""

        confidence = self._confidence(sources)
        draft_path = self._write_draft(
            task_id=task_id,
            specialty=specialty,
            slug=slug,
            answer=answer,
            reasoning=tuple(reasoning),
            sources=tuple(sources),
            confidence=confidence,
        )
        return GroundedDraft(
            task_id=task_id,
            specialty=specialty,
            answer=answer,
            reasoning=tuple(reasoning),
            sources=tuple(sources),
            confidence=confidence,
            tokens_used=tokens,
            model=model,
            draft_path=draft_path,
        )

    async def _run_tool_call(self, call: object, sources: list[FetchedSource]) -> str:
        """Execute one tool call, appending any fetched source. Returns the
        (untrusted-wrapped) tool output for the model, or an error string."""
        name = getattr(call, "name", "")
        args = getattr(call, "arguments", {}) or {}
        if name != "web_fetch":
            return f"Error: unknown tool {name!r}"
        url = str(args.get("url", "")).strip()
        if not url:
            return "Error: web_fetch requires a 'url' argument"
        try:
            source = await web_fetch(
                url,
                allowlist=self._allowlist,
                fetcher=self._fetcher,
                now_ms=self._now_ms,
                source_id=f"src-{len(sources) + 1}",
            )
        except WebFetchNotAllowedError as exc:
            return f"Error: {exc}"
        sources.append(source)
        # Fetched web content is untrusted — wrap it so the model treats it as
        # data, not instructions (indirect prompt-injection mitigation).
        return wrap_untrusted(f"[{source.id}] {source.title} ({source.url})\n{source.text}")

    @staticmethod
    def _confidence(sources: list[FetchedSource]) -> float:
        """Cheap heuristic: ungrounded answers are low-confidence; confidence
        rises with the number of grounding sources, capped well under 1.0."""
        if not sources:
            return 0.1
        return round(min(0.9, 0.4 + 0.15 * len(sources)), 2)

    def _write_draft(
        self,
        *,
        task_id: str,
        specialty: str,
        slug: str,
        answer: str,
        reasoning: tuple[str, ...],
        sources: tuple[FetchedSource, ...],
        confidence: float,
    ) -> Path:
        frontmatter = {
            "source": f"{specialty}-worker:{self._worker_id}",
            "task_id": task_id,
            "specialty": specialty,
            "confidence": confidence,
            "critic_score": 0.0,  # filled in by morning curation / later critic
        }
        return self._writer.write(
            frontmatter=frontmatter,
            sources=[s.to_frontmatter() for s in sources],
            body=_render_body(answer, reasoning, sources),
            slug=slug,
        )


def _render_body(
    answer: str, reasoning: tuple[str, ...], sources: tuple[FetchedSource, ...]
) -> str:
    parts = ["# Answer", "", answer, "", "## Reasoning", ""]
    parts.extend(f"{i}. {step}" for i, step in enumerate(reasoning, start=1))
    parts.extend(["", "## Sources", ""])
    parts.extend(f"- [{s.id}] {s.title} — {s.url}" for s in sources)
    return "\n".join(parts)


def build_grounded_episode(
    draft: GroundedDraft,
    *,
    subtask_id: str,
    worker_id: str,
    latency_ms: int,
    recorded_at_ms: int,
    adapter_version: str = "",
    outcome: SubtaskState = SubtaskState.COMPLETED,
) -> Episode:
    """Build an episode from a grounded draft.

    The episode's ``trajectory`` is the **reasoning steps**, not just the final
    answer — that captured reasoning is the reasoning-bearing SFT target ADR
    0009 requires.
    """
    return Episode(
        task_id=draft.task_id,
        subtask_id=subtask_id,
        worker_id=worker_id,
        specialty=draft.specialty,
        model_version=draft.model,
        adapter_version=adapter_version,
        input_text="",  # the dispatched prompt is recorded by the dispatcher
        trajectory=draft.reasoning,
        output_text=draft.answer,
        success=outcome is SubtaskState.COMPLETED,
        latency_ms=latency_ms,
        tokens_used=draft.tokens_used,
        outcome=outcome,
        critic_score=0.0,
        recorded_at_ms=recorded_at_ms,
    )
