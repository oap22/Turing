"""CriticQueue — async backpressured queue scoring closed episodes.

A background worker drains the queue: pulls one episode, scores it via the
local critic, writes the score back to the EpisodeStore. With probability
`calibration_sample_rate` (default 0.05) it also routes to the calibration
critic so the DriftDetector can compare local vs. cloud.

Backpressure: `asyncio.Queue(maxsize=max_size)` makes `enqueue` block when
the queue is full, so a producer can't outrun the critic.

Ordering: FIFO is preserved by `asyncio.Queue`'s internal deque.

`start()` is idempotent; `drain()` waits until every enqueued episode has
been processed via `Queue.join()` — no polling, no timing assumptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import random
    from collections.abc import Sequence

    from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
    from turing.learning.critic.critic import Critic

# Production backoffs from ADR 0004 §6 (30s / 2m / 10m).
_DEFAULT_BACKOFFS_S: tuple[float, ...] = (30.0, 120.0, 600.0)
_DEFAULT_MAX_ATTEMPTS = 3

_log = logging.getLogger(__name__)


class CriticQueue:
    def __init__(
        self,
        *,
        local_critic: Critic,
        calibration_critic: Critic | None,
        episode_store: EpisodeStore,
        rng: random.Random,
        calibration_sample_rate: float = 0.05,
        max_size: int = 1024,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: Sequence[float] = _DEFAULT_BACKOFFS_S,
    ) -> None:
        if not 0.0 <= calibration_sample_rate <= 1.0:
            raise ValueError("calibration_sample_rate must be in [0, 1]")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._local = local_critic
        self._cloud = calibration_critic
        self._store = episode_store
        self._rng = rng
        self._sample_rate = calibration_sample_rate
        self._max_attempts = max_attempts
        self._backoffs = tuple(backoff_seconds)
        self._queue: asyncio.Queue[Episode] = asyncio.Queue(maxsize=max_size)
        self._worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    async def enqueue(self, episode: Episode) -> None:
        self.start()
        await self._queue.put(episode)

    def enqueue_nowait(self, episode: Episode) -> None:
        """Sync push for sync call sites (e.g. lifecycle hooks).

        Raises ``asyncio.QueueFull`` instead of blocking — backpressure
        surfaces as a hard error so callers know the queue is saturated
        rather than dropping silently.
        """
        self.start()
        self._queue.put_nowait(episode)

    async def drain(self) -> None:
        self.start()
        await self._queue.join()

    async def _worker_loop(self) -> None:
        while True:
            episode = await self._queue.get()
            try:
                await self._score_one(episode)
            finally:
                self._queue.task_done()

    async def _score_one(self, episode: Episode) -> None:
        """Run up to ``max_attempts`` against the local critic; mark failed
        on exhaustion per ADR 0004 §6. Calibration sampling is best-effort
        on top of a successful local score; calibration failures are not
        retried (drift detector tolerates sample gaps)."""
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                local_score = await self._local.score(episode)
            except Exception as exc:
                last_error = exc
                _log.warning(
                    "critic attempt %d/%d failed for subtask_id=%s: %s",
                    attempt,
                    self._max_attempts,
                    episode.subtask_id,
                    exc,
                )
                if attempt < self._max_attempts:
                    backoff = self._backoffs[
                        min(attempt - 1, len(self._backoffs) - 1)
                    ]
                    if backoff > 0:
                        await asyncio.sleep(backoff)
                continue
            # Success path.
            self._store.update_critic_score(
                subtask_id=episode.subtask_id, critic_score=local_score.overall
            )
            if self._cloud is not None and self._rng.random() < self._sample_rate:
                with contextlib.suppress(Exception):
                    await self._cloud.score(episode)
            return

        # Retry budget exhausted.
        _log.error(
            "critic retry budget exhausted for subtask_id=%s; marking failed (%s)",
            episode.subtask_id,
            last_error,
        )
        self._store.mark_critic_failed(subtask_id=episode.subtask_id)
