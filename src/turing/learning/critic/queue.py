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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import random

    from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
    from turing.learning.critic.critic import Critic


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
    ) -> None:
        if not 0.0 <= calibration_sample_rate <= 1.0:
            raise ValueError("calibration_sample_rate must be in [0, 1]")
        self._local = local_critic
        self._cloud = calibration_critic
        self._store = episode_store
        self._rng = rng
        self._sample_rate = calibration_sample_rate
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
        local_score = await self._local.score(episode)
        self._store.update_critic_score(
            subtask_id=episode.subtask_id, critic_score=local_score.overall
        )

        if self._cloud is None:
            return
        if self._rng.random() < self._sample_rate:
            await self._cloud.score(episode)
