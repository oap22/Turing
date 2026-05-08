"""Workers for the research-summarize eval harness.

A `WorkerFn` takes a case and returns the worker's summary output. The
harness scores whatever string comes back, so workers are responsible for
formatting source docs into the prompt the underlying system expects.

`direct_worker` calls an `LLMProvider` directly — useful for local
debugging and CI smoke tests. The coordinator-side worker (NATS dispatch)
is tracked separately, since it depends on the orchestrator's subtask
wire format which is not yet finalised.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from turing.llm.base import Message, Role

if TYPE_CHECKING:
    from collections.abc import Callable

    from turing.llm.base import LLMProvider

    from .schema import EvalCase

    WorkerFn = Callable[["EvalCase"], str]


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
