"""Critic wiring: lifecycle terminal hook + startup backfill (#117 follow-up)."""

from __future__ import annotations

import asyncio
import random

import pytest

from turing.coordinator.lifecycle.episode_store import (
    CriticStatus,
    Episode,
    EpisodeStore,
)
from turing.coordinator.lifecycle.lifecycle import (
    SubtaskEvent,
    SubtaskState,
    TaskLifecycle,
)
from turing.learning.critic.critic import CriticScore
from turing.learning.critic.queue import CriticQueue
from turing.learning.critic.wiring import (
    backfill_pending_critic,
    make_terminal_critic_hook,
)


def _episode(*, subtask_id: str, outcome: SubtaskState, recorded_at_ms: int = 0) -> Episode:
    return Episode(
        task_id="t",
        subtask_id=subtask_id,
        worker_id="w",
        specialty="x",
        model_version="m",
        adapter_version="a",
        input_text="i",
        trajectory=("s",),
        output_text="o",
        success=outcome is SubtaskState.COMPLETED,
        latency_ms=1,
        tokens_used=1,
        outcome=outcome,
        critic_score=0.0,
        recorded_at_ms=recorded_at_ms,
    )


class _ScoringCritic:
    async def score(self, episode):
        return CriticScore(
            correctness=0.5, efficiency=0.5, specialty_fit=0.5, critique=""
        )


# ── Lifecycle terminal hook ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_completed_transition_enqueues_episode_for_scoring():
    store = EpisodeStore()
    store.record(_episode(subtask_id="s-1", outcome=SubtaskState.COMPLETED))

    queue = CriticQueue(
        local_critic=_ScoringCritic(),
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
    )
    hook = make_terminal_critic_hook(episode_store=store, critic_queue=queue)
    lifecycle = TaskLifecycle(on_terminal=hook)

    lifecycle.apply(SubtaskEvent(subtask_id="s-1", new_state=SubtaskState.COMPLETED, ts_ms=1))
    queue.start()
    await queue.drain()
    await queue.stop()

    assert store.critic_status_of("s-1") is CriticStatus.SCORED


@pytest.mark.asyncio
async def test_rejected_transition_does_not_enqueue():
    store = EpisodeStore()
    store.record(_episode(subtask_id="s-2", outcome=SubtaskState.REJECTED))

    enqueued: list[str] = []

    class _SpyCritic:
        async def score(self, ep):
            enqueued.append(ep.subtask_id)
            return CriticScore(correctness=0.5, efficiency=0.5, specialty_fit=0.5, critique="")

    queue = CriticQueue(
        local_critic=_SpyCritic(),
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
    )
    hook = make_terminal_critic_hook(episode_store=store, critic_queue=queue)
    lifecycle = TaskLifecycle(on_terminal=hook)
    # PENDING -> REJECTED is allowed by the lifecycle.
    lifecycle.apply(SubtaskEvent(subtask_id="s-2", new_state=SubtaskState.REJECTED, ts_ms=1))

    queue.start()
    # Give scheduler a tick — nothing should have been enqueued.
    await asyncio.sleep(0)
    await queue.stop()

    assert enqueued == []
    assert store.critic_status_of("s-2") is CriticStatus.PENDING


@pytest.mark.asyncio
async def test_failed_and_timed_out_transitions_enqueue():
    store = EpisodeStore()
    store.record(_episode(subtask_id="s-f", outcome=SubtaskState.FAILED))
    store.record(_episode(subtask_id="s-t", outcome=SubtaskState.TIMED_OUT))

    queue = CriticQueue(
        local_critic=_ScoringCritic(),
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
    )
    hook = make_terminal_critic_hook(episode_store=store, critic_queue=queue)
    lifecycle = TaskLifecycle(on_terminal=hook)
    # Walk through allowed edges to terminal states.
    for sid, terminal in [("s-f", SubtaskState.FAILED), ("s-t", SubtaskState.TIMED_OUT)]:
        lifecycle.apply(SubtaskEvent(subtask_id=sid, new_state=SubtaskState.PENDING, ts_ms=1))
        lifecycle.apply(SubtaskEvent(subtask_id=sid, new_state=SubtaskState.DISPATCHED, ts_ms=2))
        lifecycle.apply(SubtaskEvent(subtask_id=sid, new_state=terminal, ts_ms=3))

    queue.start()
    await queue.drain()
    await queue.stop()

    assert store.critic_status_of("s-f") is CriticStatus.SCORED
    assert store.critic_status_of("s-t") is CriticStatus.SCORED


@pytest.mark.asyncio
async def test_terminal_idempotent_only_enqueues_once():
    store = EpisodeStore()
    store.record(_episode(subtask_id="s-x", outcome=SubtaskState.COMPLETED))

    score_calls: list[str] = []

    class _CountingCritic:
        async def score(self, ep):
            score_calls.append(ep.subtask_id)
            return CriticScore(correctness=0.6, efficiency=0.6, specialty_fit=0.6, critique="")

    queue = CriticQueue(
        local_critic=_CountingCritic(),
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
    )
    hook = make_terminal_critic_hook(episode_store=store, critic_queue=queue)
    lifecycle = TaskLifecycle(on_terminal=hook)
    # Repeated COMPLETED apply is a no-op for state — but we want the hook
    # to also not double-fire.
    lifecycle.apply(SubtaskEvent(subtask_id="s-x", new_state=SubtaskState.COMPLETED, ts_ms=1))
    lifecycle.apply(SubtaskEvent(subtask_id="s-x", new_state=SubtaskState.COMPLETED, ts_ms=2))

    queue.start()
    await queue.drain()
    await queue.stop()

    assert score_calls == ["s-x"]


# ── Backfill ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_enqueues_pending_within_horizon():
    store = EpisodeStore()
    now = 10_000_000
    horizon = 1_000
    # Inside horizon
    store.record(_episode(subtask_id="recent", outcome=SubtaskState.COMPLETED, recorded_at_ms=now - 100))
    # Outside horizon
    store.record(_episode(subtask_id="old", outcome=SubtaskState.COMPLETED, recorded_at_ms=now - 5_000))

    queue = CriticQueue(
        local_critic=_ScoringCritic(),
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
    )
    pushed = await backfill_pending_critic(
        episode_store=store, critic_queue=queue, now_ms=now, horizon_ms=horizon
    )
    assert pushed == 1

    queue.start()
    await queue.drain()
    await queue.stop()

    assert store.critic_status_of("recent") is CriticStatus.SCORED
    assert store.critic_status_of("old") is CriticStatus.PENDING


@pytest.mark.asyncio
async def test_backfill_dedupes_via_shared_set():
    store = EpisodeStore()
    now = 10_000
    store.record(_episode(subtask_id="s-a", outcome=SubtaskState.COMPLETED, recorded_at_ms=now))

    queue = CriticQueue(
        local_critic=_ScoringCritic(),
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
    )

    seen: set[str] = set()
    first = await backfill_pending_critic(
        episode_store=store,
        critic_queue=queue,
        now_ms=now,
        horizon_ms=1_000_000,
        already_enqueued=seen,
    )
    second = await backfill_pending_critic(
        episode_store=store,
        critic_queue=queue,
        now_ms=now,
        horizon_ms=1_000_000,
        already_enqueued=seen,
    )
    assert first == 1
    assert second == 0  # already enqueued, so backfill skips
