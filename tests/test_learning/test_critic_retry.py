"""CriticQueue retry-then-fail per ADR 0004 §6.

Three attempts on the coordinator side; after exhaustion the episode is
marked ``critic_status='failed'`` and ``critic_score`` stays at its
default. Backfill exclusion of failed rows is the EpisodeStore's job
(tested separately); this slice tests the queue's retry-and-mark logic.

Backoffs (30s/2m/10m) are configurable to 0 in tests so we don't sleep.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from turing.coordinator.lifecycle import SubtaskState
from turing.coordinator.lifecycle.episode_store import (
    CriticStatus,
    Episode,
    EpisodeStore,
)
from turing.learning.critic import CriticQueue, CriticScore
from turing.learning.critic.critic import Critic


def _episode(subtask_id: str = "sub-1") -> Episode:
    return Episode(
        task_id="task-1",
        subtask_id=subtask_id,
        worker_id="worker-1",
        specialty="research-summarize",
        model_version="qwen2.5:14b",
        adapter_version="base",
        input_text="prompt",
        trajectory=("act-1",),
        output_text="result",
        success=True,
        latency_ms=1,
        tokens_used=1,
        outcome=SubtaskState.COMPLETED,
        critic_score=0.0,
        recorded_at_ms=0,
    )


@dataclass
class _AlwaysFailCritic(Critic):
    calls: int = 0

    async def score(self, episode: Episode) -> CriticScore:
        self.calls += 1
        raise RuntimeError("simulated critic failure")


@dataclass
class _FailThenSucceedCritic(Critic):
    fail_until: int = 2  # fail on attempts 1..fail_until, succeed after
    calls: int = 0

    async def score(self, episode: Episode) -> CriticScore:
        self.calls += 1
        if self.calls <= self.fail_until:
            raise RuntimeError(f"transient failure #{self.calls}")
        return CriticScore(
            correctness=0.7, efficiency=0.7, specialty_fit=0.7, critique="ok"
        )


@pytest.mark.asyncio
async def test_three_failures_mark_episode_failed_and_stop() -> None:
    store = EpisodeStore()
    store.record(_episode("st1"))
    critic = _AlwaysFailCritic()
    queue = CriticQueue(
        local_critic=critic,
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
        max_attempts=3,
        backoff_seconds=(0.0, 0.0, 0.0),
    )
    await queue.enqueue(_episode("st1"))
    await queue.drain()
    await queue.stop()

    assert critic.calls == 3  # exactly 3 attempts, no more
    assert store.critic_status_of("st1") is CriticStatus.FAILED


@pytest.mark.asyncio
async def test_transient_failures_recover_within_budget() -> None:
    store = EpisodeStore()
    store.record(_episode("st1"))
    critic = _FailThenSucceedCritic(fail_until=2)
    queue = CriticQueue(
        local_critic=critic,
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
        max_attempts=3,
        backoff_seconds=(0.0, 0.0, 0.0),
    )
    await queue.enqueue(_episode("st1"))
    await queue.drain()
    await queue.stop()

    # 2 failures + 1 success = 3 calls.
    assert critic.calls == 3
    assert store.critic_status_of("st1") is CriticStatus.SCORED


@pytest.mark.asyncio
async def test_successful_first_attempt_does_not_retry() -> None:
    store = EpisodeStore()
    store.record(_episode("st1"))
    critic = _FailThenSucceedCritic(fail_until=0)  # never fails
    queue = CriticQueue(
        local_critic=critic,
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
        max_attempts=3,
        backoff_seconds=(0.0, 0.0, 0.0),
    )
    await queue.enqueue(_episode("st1"))
    await queue.drain()
    await queue.stop()

    assert critic.calls == 1
    assert store.critic_status_of("st1") is CriticStatus.SCORED
