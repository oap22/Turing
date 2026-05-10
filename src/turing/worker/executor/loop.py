"""Worker subscriber loop — receives SubtaskDispatch, runs executor, replies.

Per ADR 0002 slice 2:

  - Subscribes to `subtasks.<specialty>` (queue) AND `subtasks.workers.<id>`
    (point-to-point) for every specialty in the worker's manifest.
  - Verifies inbound MeshMessage signature (delegated to SignedTransport).
  - Rejects specialty mismatches without invoking the executor.
  - Self-cancels at deadline_ms; emits TIMED_OUT.
  - Idempotent on subtask_id: a second delivery returns the cached result
    without re-running the executor (JetStream redelivery is at-least-once).

The `Executor` callable is injected so tests pass a fake; production wires
the existing `agent/core.py` Perceive→Think→Act→Remember loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from turing.coordinator.dispatch import SubtaskDispatch, TaskResult
from turing.transport.envelope import MeshMessage

if TYPE_CHECKING:
    from turing.transport.signed_transport import SignedTransport

logger = logging.getLogger(__name__)

_MIN_DEADLINE_BUDGET_S = (
    0.05  # floor so a stale deadline still gives the executor a chance to start
)


@dataclass(frozen=True)
class ExecutorOutput:
    """Successful executor result.

    NEEDS_SUBTASK is signalled by raising `NeedsSubtask` rather than
    returning here, so the executor never has to remember to set status
    correctly.
    """

    output: str
    tokens_used: int
    model: str
    latency_ms: int = 0

    @classmethod
    def completed(cls, *, output: str, tokens_used: int, model: str) -> ExecutorOutput:
        return cls(output=output, tokens_used=tokens_used, model=model)


class NeedsSubtask(Exception):  # noqa: N818 - control-flow signal, not an error
    """Raised by the executor to extend the DAG (per ADR 0001)."""

    def __init__(self, *, reason: str, fragment: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fragment = fragment


Executor = Callable[[SubtaskDispatch], Awaitable[ExecutorOutput]]


class WorkerLoop:
    def __init__(
        self,
        *,
        transport: SignedTransport,
        worker_id: str,
        specialties: tuple[str, ...],
        executor: Executor,
        now_ms: Callable[[], int],
    ) -> None:
        self._transport = transport
        self._worker_id = worker_id
        self._specialties = frozenset(specialties)
        self._executor = executor
        self._now_ms = now_ms
        # subtask_id -> cached TaskResult for idempotent redelivery.
        self._cache: dict[str, TaskResult] = {}
        # subtask_id -> in-flight Task handling that subtask, so a duplicate
        # arriving while the first is still running waits on it instead of
        # spawning a second executor call.
        self._inflight: dict[str, asyncio.Task[TaskResult]] = {}

    async def start(self) -> None:
        """Subscribe to dispatch subjects. Must be awaited before traffic."""
        for specialty in self._specialties:
            await self._transport.subscribe(
                f"subtasks.{specialty}",
                self._handle,
                on_error=lambda exc, s=specialty: logger.warning(  # type: ignore[misc]
                    "subtasks.%s subscribe error: %s", s, exc
                ),
            )
        await self._transport.subscribe(
            f"subtasks.workers.{self._worker_id}",
            self._handle,
            on_error=lambda exc: logger.warning("worker-direct subscribe error: %s", exc),
        )

    async def _handle(self, msg: MeshMessage) -> None:
        try:
            envelope = SubtaskDispatch.from_dict(json.loads(msg.payload.decode("utf-8")))
        except (ValueError, KeyError) as exc:
            logger.warning("dropping malformed dispatch: %s", exc)
            return

        # Idempotency: if we've already produced a result, re-emit it.
        cached = self._cache.get(envelope.subtask_id)
        if cached is not None:
            await self._publish_result(cached)
            return

        # If we're already running this subtask, wait for that run to finish
        # and re-emit its result. (Redelivery during execution.)
        running = self._inflight.get(envelope.subtask_id)
        if running is not None:
            try:
                result = await running
            except Exception:  # the run task already published its own result
                return
            await self._publish_result(result)
            return

        task = asyncio.create_task(self._run_one(envelope))
        self._inflight[envelope.subtask_id] = task
        try:
            await task
        finally:
            self._inflight.pop(envelope.subtask_id, None)

    async def _run_one(self, envelope: SubtaskDispatch) -> TaskResult:
        # Specialty gate.
        if envelope.specialty not in self._specialties:
            result = TaskResult(
                subtask_id=envelope.subtask_id,
                worker_id=self._worker_id,
                status="REJECTED",
                output="",
                tokens_used=0,
                latency_ms=0,
                model="",
                error=(
                    f"specialty {envelope.specialty!r} not in manifest {sorted(self._specialties)}"
                ),
            )
            self._cache[envelope.subtask_id] = result
            await self._publish_result(result)
            return result

        # Self-timeout at deadline_ms; small floor so a slightly-stale
        # deadline still lets the executor at least start.
        budget_s = max(
            _MIN_DEADLINE_BUDGET_S,
            (envelope.deadline_ms - self._now_ms()) / 1000.0,
        )

        start_ms = self._now_ms()
        try:
            output = await asyncio.wait_for(self._executor(envelope), timeout=budget_s)
            result = TaskResult(
                subtask_id=envelope.subtask_id,
                worker_id=self._worker_id,
                status="COMPLETED",
                output=output.output,
                tokens_used=output.tokens_used,
                latency_ms=self._now_ms() - start_ms,
                model=output.model,
            )
        except TimeoutError:
            result = TaskResult(
                subtask_id=envelope.subtask_id,
                worker_id=self._worker_id,
                status="TIMED_OUT",
                output="",
                tokens_used=0,
                latency_ms=self._now_ms() - start_ms,
                model="",
                error=f"executor exceeded deadline ({budget_s:.3f}s)",
            )
        except NeedsSubtask as exc:
            result = TaskResult(
                subtask_id=envelope.subtask_id,
                worker_id=self._worker_id,
                status="NEEDS_SUBTASK",
                output="",
                tokens_used=0,
                latency_ms=self._now_ms() - start_ms,
                model="",
                error=exc.reason,
                fragment=exc.fragment,
            )
        except Exception as exc:  # any other error -> FAILED
            result = TaskResult(
                subtask_id=envelope.subtask_id,
                worker_id=self._worker_id,
                status="FAILED",
                output="",
                tokens_used=0,
                latency_ms=self._now_ms() - start_ms,
                model="",
                error=f"{type(exc).__name__}: {exc}",
            )

        self._cache[envelope.subtask_id] = result
        await self._publish_result(result)
        return result

    async def _publish_result(self, result: TaskResult) -> None:
        msg = MeshMessage(
            request_id=f"result-{result.subtask_id}-{self._now_ms()}",
            sender_id=self._worker_id,
            subject=f"subtasks.{result.subtask_id}.result",
            payload=json.dumps(result.to_dict()).encode("utf-8"),
            timestamp_ms=self._now_ms(),
        )
        await self._transport.publish(msg)
