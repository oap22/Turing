"""GroundedResearchHandler — the worker's handler for RESEARCH subtasks (#261).

The missing wire of the Phase 0 walking skeleton: when the coordinator
dispatches a ``SubtaskKind.RESEARCH`` subtask, the worker runs
:class:`~turing.worker.grounding.GroundedResearcher` (Think→Act with
``web_fetch`` grounding), writes the inbox draft, and returns a
:class:`~turing.coordinator.dispatch.TaskResult` whose ``fragment`` carries the
**reasoning trajectory** back so the coordinator's episode is reasoning-bearing
— not just the final answer.

Register it on the worker's :class:`~turing.worker.executor.kind_router.KindRouter`
under ``SubtaskKind.RESEARCH``. The LLM, fetcher, allowlist, and inbox writer
are all injected via the supplied :class:`GroundedResearcher`, so the handler is
fully testable with a stub provider + stub fetcher and a tmp vault.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turing.coordinator.dispatch import (
    SubtaskDispatch,
    TaskResult,
    grounding_to_fragment,
    merge_fragments,
)
from turing.coordinator.lifecycle.lifecycle import SubtaskState

if TYPE_CHECKING:
    from collections.abc import Callable

    from turing.worker.grounding import GroundedResearcher


class GroundedResearchHandler:
    """Runs grounded research for a dispatched subtask and reports the result."""

    def __init__(
        self,
        *,
        researcher: GroundedResearcher,
        worker_id: str,
        now_ms: Callable[[], int],
    ) -> None:
        self._researcher = researcher
        self._worker_id = worker_id
        self._now_ms = now_ms

    async def handle(self, envelope: SubtaskDispatch) -> TaskResult:
        """Ground an answer to ``envelope.prompt`` and pack the reasoning home.

        The draft folder is keyed by ``subtask_id`` (the question id) so each
        question lands its own ``vault/inbox/<subtask_id>/`` for morning review.
        On any failure the subtask is reported ``FAILED`` with the error string,
        mirroring the dispatcher's existing failure→episode path.
        """
        started = self._now_ms()
        try:
            draft = await self._researcher.research(
                task_id=envelope.subtask_id,
                prompt=envelope.prompt,
                specialty=envelope.specialty,
            )
        except Exception as exc:  # report, don't crash the worker
            return TaskResult(
                subtask_id=envelope.subtask_id,
                worker_id=self._worker_id,
                status=SubtaskState.FAILED.value,
                output="",
                tokens_used=0,
                latency_ms=self._now_ms() - started,
                model="",
                error=str(exc),
            )

        fragment = merge_fragments(
            grounding_to_fragment(
                reasoning=draft.reasoning,
                confidence=draft.confidence,
                source_count=len(draft.sources),
            )
        )
        return TaskResult(
            subtask_id=envelope.subtask_id,
            worker_id=self._worker_id,
            status=SubtaskState.COMPLETED.value,
            output=draft.answer,
            tokens_used=draft.tokens_used,
            latency_ms=self._now_ms() - started,
            model=draft.model,
            fragment=fragment,
        )
