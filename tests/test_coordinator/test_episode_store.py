"""Tests for EpisodeStore — the SFT/DPO-ready corpus.

Episode rows land here on terminal-state transitions. Phase A/B/C consumers
query by specialty + score + recency. REJECTED rows are kept for audit but
flagged so positive-training queries skip them.
"""

from __future__ import annotations

import pytest

from turing.coordinator.lifecycle import SubtaskState
from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore


def _episode(
    *,
    subtask_id: str = "sub-1",
    specialty: str = "research-summarize",
    outcome: SubtaskState = SubtaskState.COMPLETED,
    critic_score: float = 0.8,
    ts_ms: int = 0,
) -> Episode:
    return Episode(
        task_id="task-1",
        subtask_id=subtask_id,
        worker_id="worker-1",
        specialty=specialty,
        model_version="qwen2.5:14b",
        adapter_version="base",
        input_text="prompt",
        trajectory=("act-1",),
        output_text="result",
        success=outcome is SubtaskState.COMPLETED,
        latency_ms=120,
        tokens_used=42,
        outcome=outcome,
        critic_score=critic_score,
        recorded_at_ms=ts_ms,
    )


def test_record_then_query_by_specialty_returns_episode() -> None:
    store = EpisodeStore()
    store.record(_episode(specialty="research-summarize"))
    store.record(_episode(subtask_id="sub-2", specialty="code-review"))

    found = store.query(specialty="research-summarize")

    assert [e.subtask_id for e in found] == ["sub-1"]


def test_query_filters_by_min_critic_score() -> None:
    store = EpisodeStore()
    store.record(_episode(subtask_id="sub-low", critic_score=0.2))
    store.record(_episode(subtask_id="sub-high", critic_score=0.9))

    found = store.query(specialty="research-summarize", min_score=0.5)

    assert [e.subtask_id for e in found] == ["sub-high"]


def test_query_orders_by_recency_descending() -> None:
    store = EpisodeStore()
    store.record(_episode(subtask_id="sub-old", ts_ms=100))
    store.record(_episode(subtask_id="sub-new", ts_ms=500))
    store.record(_episode(subtask_id="sub-mid", ts_ms=300))

    found = store.query(specialty="research-summarize")

    assert [e.subtask_id for e in found] == ["sub-new", "sub-mid", "sub-old"]


def test_query_respects_limit() -> None:
    store = EpisodeStore()
    for i in range(5):
        store.record(_episode(subtask_id=f"sub-{i}", ts_ms=i))

    found = store.query(specialty="research-summarize", limit=2)
    assert len(found) == 2


def test_rejected_episodes_excluded_from_positive_training_corpus() -> None:
    store = EpisodeStore()
    store.record(_episode(subtask_id="sub-ok", outcome=SubtaskState.COMPLETED))
    store.record(_episode(subtask_id="sub-bad", outcome=SubtaskState.REJECTED))

    positive = store.query(specialty="research-summarize", positive_only=True)

    assert [e.subtask_id for e in positive] == ["sub-ok"]


def test_rejected_episodes_remain_in_audit_query() -> None:
    store = EpisodeStore()
    store.record(_episode(subtask_id="sub-bad", outcome=SubtaskState.REJECTED))

    all_rows = store.query(specialty="research-summarize")
    assert [e.subtask_id for e in all_rows] == ["sub-bad"]


def test_record_rejects_non_terminal_outcome() -> None:
    store = EpisodeStore()

    with pytest.raises(ValueError, match="terminal"):
        store.record(_episode(outcome=SubtaskState.RUNNING))


def test_record_is_idempotent_on_subtask_id() -> None:
    """Story 15: duplicate episode on retry of same subtask_id is a no-op."""
    store = EpisodeStore()
    store.record(_episode(subtask_id="sub-1", critic_score=0.5))
    store.record(_episode(subtask_id="sub-1", critic_score=0.9))  # duplicate

    found = store.query(specialty="research-summarize")
    assert len(found) == 1
    assert found[0].critic_score == 0.5  # first write wins
