"""SubtaskDispatchClient — publishes a SubtaskDispatch and awaits a TaskResult.

Per ADR 0002, the client subscribes to `subtasks.<subtask_id>.result` BEFORE
publishing the dispatch so the reply correlation is race-free. Two dispatch
subjects are supported:

  - `subtasks.<specialty>` (queue-balanced normal traffic)
  - `subtasks.workers.<worker_id>` (point-to-point; eval / promotion testing)

Worker selection is the caller's call: pass `worker_id=None` for queue,
or a worker id for direct.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING

from turing.transport.envelope import MeshMessage

from .envelopes import SubtaskDispatch, TaskResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from turing.transport.signed_transport import SignedTransport

logger = logging.getLogger(__name__)


class SubtaskTimeoutError(TimeoutError):
    """Raised when no TaskResult arrives within deadline + grace."""


class SubtaskDispatchClient:
    def __init__(
        self,
        *,
        transport: SignedTransport,
        sender_id: str,
        now_ms: Callable[[], int],
    ) -> None:
        self._transport = transport
        self._sender_id = sender_id
        self._now_ms = now_ms
        self.now_ms = now_ms  # exposed so callers using deadlines share the clock
        # Map subtask_id -> Future awaiting that subtask's result.
        self._pending: dict[str, asyncio.Future[TaskResult]] = {}

    async def dispatch(
        self,
        envelope: SubtaskDispatch,
        *,
        worker_id: str | None = None,
        deadline_ms: int,
        grace_s: float = 30.0,
    ) -> TaskResult:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[TaskResult] = loop.create_future()
        self._pending[envelope.subtask_id] = future

        result_subject = f"subtasks.{envelope.subtask_id}.result"

        async def _on_result(msg: MeshMessage) -> None:
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
                result = TaskResult.from_dict(payload)
            except (ValueError, KeyError) as exc:
                logger.warning(
                    "dropping malformed result for %s: %s",
                    envelope.subtask_id,
                    exc,
                )
                return
            pending = self._pending.get(result.subtask_id)
            if pending is None or pending.done():
                # Late or duplicate result — log and drop. The caller has
                # already seen its timeout (or earlier success).
                logger.info(
                    "dropping late/duplicate result for %s (status=%s)",
                    result.subtask_id,
                    result.status,
                )
                return
            pending.set_result(result)

        await self._transport.subscribe(
            result_subject,
            _on_result,
            on_error=lambda exc: logger.warning(
                "result-subject error for %s: %s", envelope.subtask_id, exc
            ),
        )

        dispatch_subject = (
            f"subtasks.workers.{worker_id}"
            if worker_id is not None
            else f"subtasks.{envelope.specialty}"
        )

        message = MeshMessage(
            request_id=str(uuid.uuid4()),
            sender_id=self._sender_id,
            subject=dispatch_subject,
            payload=json.dumps(envelope.to_dict()).encode("utf-8"),
            timestamp_ms=self._now_ms(),
        )
        await self._transport.publish(message)

        # Compute timeout: deadline_ms is wall-clock; grace is added on top.
        # We do NOT use deadline directly because tests fix now_ms; just use
        # the grace as the absolute upper bound from this point.
        timeout_s = max(grace_s, (deadline_ms - self._now_ms()) / 1000.0 + grace_s)
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError as exc:
            raise SubtaskTimeoutError(
                f"no result for {envelope.subtask_id} within {timeout_s}s"
            ) from exc
        finally:
            # Don't pop until either we got the result or timed out — late
            # results landing here can still be dropped quietly via the
            # done-check above.
            pending = self._pending.get(envelope.subtask_id)
            if pending is not None and pending.done():
                self._pending.pop(envelope.subtask_id, None)
