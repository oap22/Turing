"""Workers for the research-summarize eval harness.

A `WorkerFn` takes a case and returns the worker's summary output. The
harness scores whatever string comes back, so workers are responsible for
formatting source docs into the prompt the underlying system expects.

Two production-grade workers:

  - `direct_worker` calls an `LLMProvider` directly. Local smoke tests and
    CI runs that want to exercise the scoring stack against a real model.
  - `coordinator_worker` dispatches a `research-summarize` subtask through
    `SubtaskDispatchClient` (ADR 0002) and returns the worker pool's
    `TaskResult.output`. This is the path that gates Phase B promotion
    against a candidate adapter on a specific worker.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from turing.coordinator.dispatch import SourceInput, SubtaskDispatch
from turing.llm.base import Message, Role

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from turing.coordinator.dispatch import SubtaskDispatchClient
    from turing.llm.base import LLMProvider

    from .schema import EvalCase

    WorkerFn = Callable[["EvalCase"], str]
    AsyncWorkerFn = Callable[["EvalCase"], Awaitable[str]]


_SYSTEM_PROMPT = (
    "You are the research-summarize specialty in a multi-edge research cluster. "
    "Given source documents and a user prompt, produce a concise summary that "
    "preserves all material claims, matches the operator's vault voice, and "
    "cites sources inline using [[src_id]] wiki-link format. Do not invent "
    "facts not present in the sources."
)


@dataclass
class DirectWorkerConfig:
    model: str
    timeout_s: float = 60.0
    temperature: float = 0.3
    max_tokens: int = 1024
    system_prompt: str = _SYSTEM_PROMPT


def _format_user_message(case: EvalCase) -> str:
    sections = [case.prompt, "", "Sources:"]
    for doc in case.source_docs:
        header = f"[[{doc.id}]]"
        if doc.title:
            header += f" {doc.title}"
        sections.append(header)
        sections.append(doc.text)
        sections.append("")
    return "\n".join(sections).strip()


def direct_worker(
    *,
    provider: LLMProvider,
    config: DirectWorkerConfig,
) -> WorkerFn:
    """Build a sync WorkerFn that calls `provider.complete` per case.

    The returned closure is synchronous (the harness expects sync workers),
    so each call spins a fresh event loop via `asyncio.run`. Per-case
    timeouts are enforced through `asyncio.wait_for`; on timeout the
    underlying error propagates out of the worker so the harness can score
    the case as zero.
    """

    def _run(case: EvalCase) -> str:
        async def _call() -> str:
            response = await asyncio.wait_for(
                provider.complete(
                    messages=[Message(role=Role.USER, content=_format_user_message(case))],
                    system=config.system_prompt,
                    max_tokens=config.max_tokens,
                    temperature=config.temperature,
                ),
                timeout=config.timeout_s,
            )
            return response.content

        # Use a private loop so we don't mutate the default event-loop policy
        # (asyncio.run sets _set_called=True, which breaks tests elsewhere
        # that rely on the deprecated get_event_loop() auto-creation path).
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_call())
        finally:
            loop.close()

    return _run


# ── coordinator_worker — NATS dispatch via SubtaskDispatchClient ──────────


@dataclass
class CoordinatorWorkerConfig:
    specialty: str = "research-summarize"
    worker_id: str | None = None  # None -> queue group; set -> worker-direct
    deadline_offset_s: float = 120.0
    grace_s: float = 30.0


def _envelope_for(case: EvalCase, *, specialty: str, deadline_ms: int) -> SubtaskDispatch:
    return SubtaskDispatch(
        subtask_id=f"eval-{case.id}-{uuid.uuid4().hex[:8]}",
        task_id=f"eval-{case.id}",
        specialty=specialty,
        prompt=case.prompt,
        source_inputs=[
            SourceInput(id=d.id, text=d.text, url=d.url, title=d.title) for d in case.source_docs
        ],
        deadline_ms=deadline_ms,
    )


def coordinator_worker_async(
    *,
    client: SubtaskDispatchClient,
    config: CoordinatorWorkerConfig,
) -> AsyncWorkerFn:
    """Async variant — drives `client.dispatch` on the caller's event loop.

    Use this in tests so you can also schedule a stub worker subscriber on
    the same loop without cross-binding asyncio futures.
    """

    async def _run(case: EvalCase) -> str:
        # Use the client's clock so a test fixture that freezes time stays
        # consistent between deadline computation and the dispatch's own
        # timeout math.
        deadline_ms = int(client.now_ms() + config.deadline_offset_s * 1000)
        envelope = _envelope_for(case, specialty=config.specialty, deadline_ms=deadline_ms)
        result = await client.dispatch(
            envelope,
            worker_id=config.worker_id,
            deadline_ms=deadline_ms,
            grace_s=config.grace_s,
        )
        return result.output

    return _run


def coordinator_worker(
    *,
    client: SubtaskDispatchClient,
    config: CoordinatorWorkerConfig,
) -> WorkerFn:
    """Sync wrapper — runs each dispatch in a fresh private event loop.

    Used by the harness CLI. Errors (timeout, protocol mismatch) propagate
    out and the harness's `run()` catches them, scoring the case as 0.
    """
    async_fn = coordinator_worker_async(client=client, config=config)

    def _run(case: EvalCase) -> str:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(async_fn(case))
        finally:
            loop.close()

    return _run
