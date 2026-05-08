"""EpisodeStore.critic_status — pending/scored/failed state per ADR 0004 §6.

A new column distinguishes "haven't scored this yet" (eligible for backfill)
from "tried, gave up" (skipped by backfill). New episodes start at 'pending';
update_critic_score writes 'scored'; mark_critic_failed writes 'failed'.

Backfill query returns only 'pending' rows within a 24h horizon.
"""

from __future__ import annotations

import pytest

from turing.coordinator.lifecycle.episode_store import (
    CriticStatus,
    Episode,
    EpisodeStore,
)
from turing.coordinator.lifecycle.lifecycle import SubtaskState


def _episode(
    subtask_id: str,
    *,
    recorded_at_ms: int,
    outcome: SubtaskState = SubtaskState.COMPLETED,
    critic_score: float = 0.0,
) -> Episode:
    return Episode(
        task_id="t1",
        subtask_id=subtask_id,
        worker_id="w1",
        specialty="research-summarize",
        model_version="qwen2.5:14b",
        adapter_version="v1",
        input_text="in",
        trajectory=("step",),
        output_text="out",
        success=True,
        latency_ms=100,
        tokens_used=10,
        outcome=outcome,
        critic_score=critic_score,
        recorded_at_ms=recorded_at_ms,
    )


def test_recorded_episode_starts_pending() -> None:
    store = EpisodeStore()
    store.record(_episode("st1", recorded_at_ms=1000))
    assert store.critic_status_of("st1") is CriticStatus.PENDING


def test_update_critic_score_transitions_to_scored() -> None:
    store = EpisodeStore()
    store.record(_episode("st1", recorded_at_ms=1000))
    store.update_critic_score(subtask_id="st1", critic_score=0.8)
    assert store.critic_status_of("st1") is CriticStatus.SCORED


def test_mark_critic_failed_transitions_to_failed() -> None:
    store = EpisodeStore()
    store.record(_episode("st1", recorded_at_ms=1000))
    store.mark_critic_failed(subtask_id="st1")
    assert store.critic_status_of("st1") is CriticStatus.FAILED


def test_backfill_query_returns_only_pending_within_horizon() -> None:
    store = EpisodeStore()
    now_ms = 100_000_000
    horizon_ms = 24 * 60 * 60 * 1000

    # 1. Pending within horizon — yes.
    store.record(_episode("recent_pending", recorded_at_ms=now_ms - 1000))
    # 2. Pending older than horizon — no.
    store.record(
        _episode("ancient_pending", recorded_at_ms=now_ms - horizon_ms - 1000)
    )
    # 3. Already scored — no.
    store.record(_episode("scored", recorded_at_ms=now_ms - 1000))
    store.update_critic_score(subtask_id="scored", critic_score=0.7)
    # 4. Marked failed — no (poison-pill exclusion).
    store.record(_episode("poison", recorded_at_ms=now_ms - 1000))
    store.mark_critic_failed(subtask_id="poison")
    # 5. REJECTED outcome — never scored regardless.
    store.record(
        _episode(
            "rejected",
            recorded_at_ms=now_ms - 1000,
            outcome=SubtaskState.REJECTED,
        )
    )

    pending = store.backfill_unscored(now_ms=now_ms, horizon_ms=horizon_ms)
    pending_ids = {ep.subtask_id for ep in pending}
    assert pending_ids == {"recent_pending"}


def test_status_lookup_for_unknown_episode_raises() -> None:
    store = EpisodeStore()
    with pytest.raises(KeyError):
        store.critic_status_of("ghost")
