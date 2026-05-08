"""Tests for the critic queue + drift detector deep modules.

PRD stories 34, 35, 36:

- Every closed episode is scored by a local 13B+ critic on
  `correctness, efficiency, specialty_fit ∈ [0,1]` plus a free-text critique.
- ~5% of episodes are additionally scored by Claude Haiku for calibration.
- When local-vs-Claude drift exceeds threshold, the local critic is
  flagged for recalibration.

Tests use seeded RNG and fake critics so behaviour is deterministic without a
live LLM.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import pytest

from turing.coordinator.lifecycle import SubtaskState
from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
from turing.learning.critic import (
    CriticQueue,
    CriticScore,
    DriftDetector,
)
from turing.learning.critic.critic import Critic


def _episode(subtask_id: str = "sub-1", critic_score: float = 0.0) -> Episode:
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
        critic_score=critic_score,
        recorded_at_ms=0,
    )


@dataclass
class _FakeCritic(Critic):
    label: str
    overall: float = 0.7
    calls: int = 0

    async def score(self, episode: Episode) -> CriticScore:
        self.calls += 1
        return CriticScore(
            correctness=self.overall,
            efficiency=self.overall,
            specialty_fit=self.overall,
            critique=f"{self.label} for {episode.subtask_id}",
        )


def test_critic_score_rejects_out_of_range_components() -> None:
    with pytest.raises(ValueError):
        CriticScore(correctness=1.5, efficiency=0.5, specialty_fit=0.5, critique="x")
    with pytest.raises(ValueError):
        CriticScore(correctness=0.5, efficiency=-0.1, specialty_fit=0.5, critique="x")


def test_critic_score_overall_is_mean_of_components() -> None:
    score = CriticScore(correctness=0.6, efficiency=0.9, specialty_fit=0.3, critique="x")
    assert score.overall == pytest.approx((0.6 + 0.9 + 0.3) / 3)


async def test_drain_scores_every_episode_through_local_critic() -> None:
    store = EpisodeStore()
    store.record(_episode("sub-1"))
    store.record(_episode("sub-2"))

    local = _FakeCritic("local")
    queue = CriticQueue(
        local_critic=local,
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
    )

    await queue.enqueue(_episode("sub-1"))
    await queue.enqueue(_episode("sub-2"))
    await queue.drain()

    assert local.calls == 2
    rows = store.query(specialty="research-summarize")
    assert all(r.critic_score == pytest.approx(0.7) for r in rows)


async def test_calibration_sample_rate_is_statistically_correct() -> None:
    store = EpisodeStore()
    local = _FakeCritic("local", overall=0.7)
    cloud = _FakeCritic("cloud", overall=0.7)
    queue = CriticQueue(
        local_critic=local,
        calibration_critic=cloud,
        calibration_sample_rate=0.05,
        episode_store=store,
        rng=random.Random(42),
    )

    n = 2000
    for i in range(n):
        ep = _episode(f"sub-{i}")
        store.record(ep)
        await queue.enqueue(ep)
    await queue.drain()

    assert local.calls == n
    # 0.05 * 2000 = 100; allow +/-3 sigma ~ +/-30 with seed-stable RNG.
    assert 70 <= cloud.calls <= 130, f"cloud calls={cloud.calls}"


async def test_drift_detector_flags_when_local_diverges_from_cloud() -> None:
    detector = DriftDetector(window=20, threshold=0.15)

    # 20 samples with ~0.25 absolute error between local and cloud.
    for _ in range(20):
        detector.observe(local=0.4, cloud=0.65)

    assert detector.is_drifting() is True


async def test_drift_detector_quiet_when_critics_agree() -> None:
    detector = DriftDetector(window=20, threshold=0.15)
    for _ in range(20):
        detector.observe(local=0.7, cloud=0.72)

    assert detector.is_drifting() is False


async def test_drift_recalibration_callback_fires_once_per_drift_event() -> None:
    fires: list[float] = []
    detector = DriftDetector(window=10, threshold=0.1, on_drift=lambda d: fires.append(d))

    for _ in range(10):
        detector.observe(local=0.3, cloud=0.7)

    assert len(fires) == 1
    assert fires[0] == pytest.approx(0.4, abs=1e-9)


async def test_queue_enforces_max_size_via_backpressure() -> None:
    """A producer cannot outrun the critic when the queue is full."""
    store = EpisodeStore()
    release = asyncio.Event()
    calls = 0

    class _GatedCritic:
        async def score(self, episode: Episode) -> CriticScore:
            nonlocal calls
            await release.wait()
            calls += 1
            return CriticScore(correctness=0.5, efficiency=0.5, specialty_fit=0.5, critique="")

    queue = CriticQueue(
        local_critic=_GatedCritic(),
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
        max_size=2,
    )

    # Fill the queue. The worker grabs one immediately and blocks on `release`,
    # so two more saturate max_size, and the fourth must wait.
    for i in range(3):
        ep = _episode(f"sub-{i}")
        store.record(ep)
        await queue.enqueue(ep)

    fourth = _episode("sub-3")
    store.record(fourth)
    blocked = asyncio.create_task(queue.enqueue(fourth))
    await asyncio.sleep(0.05)
    assert not blocked.done(), "enqueue should block while queue is full"

    release.set()
    await blocked
    await queue.drain()
    await queue.stop()
    assert calls == 4


async def test_critic_score_written_back_to_episode_store() -> None:
    store = EpisodeStore()
    ep = _episode("sub-1", critic_score=0.0)
    store.record(ep)

    local = _FakeCritic("local", overall=0.85)
    queue = CriticQueue(
        local_critic=local,
        calibration_critic=None,
        episode_store=store,
        rng=random.Random(0),
    )
    await queue.enqueue(ep)
    await queue.drain()

    rows = store.query(specialty="research-summarize")
    assert rows[0].critic_score == pytest.approx(0.85)
