"""Critic wiring helpers — lifecycle hook + startup backfill.

Issue #117 follow-up. Two coordinator boot-time wiring points:

1. ``make_terminal_critic_hook(episode_store, critic_queue)`` returns a
   callable suitable for ``TaskLifecycle(on_terminal=...)``. On every
   COMPLETED / FAILED / TIMED_OUT transition the corresponding episode is
   pushed onto the critic queue. REJECTED is excluded by the lifecycle
   itself (audit-only per ADR 0004 §5).

2. ``backfill_pending_critic(episode_store, critic_queue, *, now_ms,
   horizon_ms)`` enqueues every episode currently in PENDING critic_status
   whose closed_at_ms falls inside the rolling window. Called once on
   coordinator startup and once per judge-worker registration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from turing.coordinator.lifecycle.episode_store import EpisodeStore
    from turing.coordinator.lifecycle.lifecycle import SubtaskState
    from turing.learning.critic.queue import CriticQueue

DEFAULT_BACKFILL_HORIZON_MS = 24 * 60 * 60 * 1000


def make_terminal_critic_hook(
    *,
    episode_store: EpisodeStore,
    critic_queue: CriticQueue,
):
    """Return a callable suitable for ``TaskLifecycle(on_terminal=...)``."""
    enqueued: set[str] = set()

    def on_terminal(subtask_id: str, _new_state: SubtaskState) -> None:
        if subtask_id in enqueued:
            return
        try:
            episode = episode_store.get(subtask_id)
        except KeyError:
            # The lifecycle event landed before EpisodeStore.record. The
            # caller is responsible for ordering (record-then-transition);
            # this branch is a safety net so a misordering doesn't crash.
            return
        critic_queue.enqueue_nowait(episode)
        enqueued.add(subtask_id)

    return on_terminal


async def backfill_pending_critic(
    *,
    episode_store: EpisodeStore,
    critic_queue: CriticQueue,
    now_ms: int,
    horizon_ms: int = DEFAULT_BACKFILL_HORIZON_MS,
    already_enqueued: set[str] | None = None,
) -> int:
    """Enqueue every PENDING-critic episode within the rolling horizon.

    ``already_enqueued`` is the in-memory de-dupe set the caller may share
    across calls (e.g. startup + per-worker-registration sweeps); each
    call mutates it with the subtask_ids it pushed.
    Returns the number of episodes enqueued by this call.
    """
    seen = already_enqueued if already_enqueued is not None else set()
    pushed = 0
    for episode in episode_store.backfill_unscored(
        now_ms=now_ms, horizon_ms=horizon_ms
    ):
        if episode.subtask_id in seen:
            continue
        await critic_queue.enqueue(episode)
        seen.add(episode.subtask_id)
        pushed += 1
    return pushed
