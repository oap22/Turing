"""Nightly dispatch of the approved question queue to the fleet (ADR 0009 §2).

The "Night" half of the Phase 0 loop. When triggered (nightly cron / scheduler),
:meth:`NightlyDispatcher.run_nightly` drains the **approved** questions off the
:class:`~turing.coordinator.flywheel.question_queue.QuestionQueue` and fans them
out **evenly across all available workers** over NATS, reusing the ADR 0002
``SubtaskDispatch`` envelope and :class:`SubtaskDispatchClient`.

The fleet is homogeneous (4× Jetson, one ``ai-ml-generalist`` specialty), so any
node can take any question. Distribution is round-robin over the workers the
:class:`~turing.coordinator.registry.CapabilityRegistry` reports live for the
question's specialty; a worker runs one question at a time via the existing
executor loop.

**Every closed subtask writes an episode row** — success or failure — because
the episode store is the training corpus and Phase 0's path to autonomy needs
both the overnight run and (later) the morning decision logged per episode.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from turing.coordinator.dispatch import SubtaskDispatch
from turing.coordinator.dispatch.client import SubtaskTimeoutError
from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState

if TYPE_CHECKING:
    from collections.abc import Callable

    from turing.coordinator.dispatch.client import SubtaskDispatchClient
    from turing.coordinator.flywheel.proposed_queue import ProposedQueue
    from turing.coordinator.flywheel.question_queue import QuestionQueue, ResearchQuestion
    from turing.coordinator.lifecycle.episode_store import EpisodeStore
    from turing.coordinator.registry import CapabilityRegistry

# Default wall-clock grace for an overnight subtask: a worker grounding an
# answer in fetched sources is slow, so the deadline is generous.
_DEFAULT_DEADLINE_MS = 30 * 60 * 1000  # 30 minutes


@dataclass(frozen=True)
class NightlyRunReport:
    """Outcome of one nightly run, for logging / the morning surface."""

    batch_id: str
    dispatched: tuple[str, ...] = ()
    succeeded: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()  # no live worker for the specialty
    episode_ids: tuple[str, ...] = ()


class NightlyDispatcher:
    """Drains the approved queue and dispatches it across the fleet."""

    def __init__(
        self,
        *,
        queue: QuestionQueue,
        dispatch_client: SubtaskDispatchClient,
        episode_store: EpisodeStore,
        registry: CapabilityRegistry,
        now_ms: Callable[[], int],
        deadline_ms: int = _DEFAULT_DEADLINE_MS,
        grace_s: float = 30.0,
        proposed_queue: ProposedQueue | None = None,
    ) -> None:
        self._queue = queue
        self._dispatch = dispatch_client
        self._episodes = episode_store
        self._registry = registry
        self._now_ms = now_ms
        self._deadline_ms = deadline_ms
        self._grace_s = grace_s
        self._proposed = proposed_queue

    async def run_nightly(self, *, batch_id: str | None = None) -> NightlyRunReport:
        """Dispatch every approved question, evenly across live workers.

        Returns a :class:`NightlyRunReport`. Questions whose specialty has no
        live worker are left in the queue (un-dispatched) and reported under
        ``skipped`` so the next run retries them.
        """
        batch = batch_id or f"night-{uuid.uuid4().hex[:12]}"
        runnable = self._queue.drain_approved()

        assignments: list[tuple[ResearchQuestion, str]] = []
        skipped: list[str] = []
        # Round-robin the workers available *per specialty* so a mixed-specialty
        # queue still distributes evenly within each specialty.
        cursor: dict[str, int] = {}
        for question in runnable:
            workers = self._live_workers(question.specialty)
            if not workers:
                skipped.append(question.question_id)
                continue
            idx = cursor.get(question.specialty, 0)
            assignments.append((question, workers[idx % len(workers)]))
            cursor[question.specialty] = idx + 1

        results = await asyncio.gather(
            *(self._dispatch_one(q, worker_id, batch) for q, worker_id in assignments)
        )

        dispatched = tuple(q.question_id for q, _ in assignments)
        succeeded = tuple(qid for qid, ok in results if ok)
        failed = tuple(qid for qid, ok in results if not ok)
        return NightlyRunReport(
            batch_id=batch,
            dispatched=dispatched,
            succeeded=succeeded,
            failed=failed,
            skipped=tuple(skipped),
            episode_ids=dispatched,
        )

    def _live_workers(self, specialty: str) -> list[str]:
        """Sorted worker ids the registry reports live for ``specialty``."""
        manifests = self._registry.find_workers(specialty=specialty, exclude_busy=False)
        return sorted(m.worker_id for m in manifests)

    async def _dispatch_one(
        self, question: ResearchQuestion, worker_id: str, batch_id: str
    ) -> tuple[str, bool]:
        """Dispatch one question, record its episode, mark it dispatched."""
        started = self._now_ms()
        deadline = started + self._deadline_ms
        envelope = SubtaskDispatch(
            subtask_id=question.question_id,
            task_id=batch_id,
            specialty=question.specialty,
            prompt=question.prompt,
            source_inputs=[],
            deadline_ms=deadline,
        )
        try:
            result = await self._dispatch.dispatch(
                envelope, worker_id=worker_id, deadline_ms=deadline, grace_s=self._grace_s
            )
        except SubtaskTimeoutError:
            self._record_episode(
                question=question,
                batch_id=batch_id,
                worker_id=worker_id,
                output="",
                model="",
                tokens=0,
                latency_ms=self._now_ms() - started,
                outcome=SubtaskState.TIMED_OUT,
            )
            self._queue.mark_dispatched(question.question_id, at_ms=started)
            return question.question_id, False

        outcome = (
            SubtaskState[result.status]
            if result.status in SubtaskState.__members__
            else SubtaskState.FAILED
        )
        self._record_episode(
            question=question,
            batch_id=batch_id,
            worker_id=result.worker_id or worker_id,
            output=result.output,
            model=result.model,
            tokens=result.tokens_used,
            latency_ms=result.latency_ms or (self._now_ms() - started),
            outcome=outcome,
        )
        # Land any follow-up questions the worker *proposed* (it does not — and
        # cannot — dispatch them itself) into the holding queue for the morning
        # frontier review. Never auto-pursued in Phase 0.
        if self._proposed is not None:
            self._proposed.ingest_from_result(
                result,
                origin_task_id=batch_id,
                origin_question_id=question.question_id,
                now_ms=self._now_ms(),
                default_specialty=question.specialty,
            )
        self._queue.mark_dispatched(question.question_id, at_ms=started)
        return question.question_id, outcome is SubtaskState.COMPLETED

    def _record_episode(
        self,
        *,
        question: ResearchQuestion,
        batch_id: str,
        worker_id: str,
        output: str,
        model: str,
        tokens: int,
        latency_ms: int,
        outcome: SubtaskState,
    ) -> None:
        self._episodes.record(
            Episode(
                task_id=batch_id,
                subtask_id=question.question_id,
                worker_id=worker_id,
                specialty=question.specialty,
                model_version=model,
                adapter_version="",
                input_text=question.prompt,
                trajectory=(),
                output_text=output,
                success=outcome is SubtaskState.COMPLETED,
                latency_ms=latency_ms,
                tokens_used=tokens,
                outcome=outcome,
                critic_score=0.0,
                recorded_at_ms=self._now_ms(),
            )
        )
