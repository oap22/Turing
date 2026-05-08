"""EpisodeRewardsStore — event-table model per ADR 0006 §1.

Rewards are events, not a single column. Multiple rows per episode keyed
on (episode_id, source, recorded_at_ms). Effective reward = SUM(value).
A late thumb after a critic_fallback row stacks naturally; a cancellation
row with negated value zeros out a prior reaction.
"""

from __future__ import annotations

import pytest

from turing.coordinator.episode_rewards import (
    EpisodeRewardsStore,
    RewardEvent,
    RewardSource,
)


def test_empty_store_effective_reward_is_zero() -> None:
    store = EpisodeRewardsStore()
    assert store.effective_reward("ep1") == pytest.approx(0.0)


def test_single_subtask_thumb_writes_and_sums() -> None:
    store = EpisodeRewardsStore()
    store.append(
        RewardEvent(
            episode_id="ep1",
            source=RewardSource.SUBTASK_THUMB,
            value=1.0,
            recorded_at_ms=1000,
        )
    )
    assert store.effective_reward("ep1") == pytest.approx(1.0)


def test_multiple_sources_stack_additively() -> None:
    """Direct +1 + synthesis-attributed +0.3 = effective +1.3 per ADR 0006 §2."""
    store = EpisodeRewardsStore()
    store.append(
        RewardEvent(
            episode_id="ep1",
            source=RewardSource.SUBTASK_THUMB,
            value=1.0,
            recorded_at_ms=1000,
        )
    )
    store.append(
        RewardEvent(
            episode_id="ep1",
            source=RewardSource.SYNTHESIS_THUMB_FRACTIONAL,
            value=0.3,
            recorded_at_ms=2000,
        )
    )
    assert store.effective_reward("ep1") == pytest.approx(1.3)


def test_late_thumb_after_critic_fallback_coexist() -> None:
    """ADR 0006 §4: late thumb after fallback fires; both rows persist."""
    store = EpisodeRewardsStore()
    store.append(
        RewardEvent(
            episode_id="ep1",
            source=RewardSource.CRITIC_FALLBACK,
            value=0.18,
            recorded_at_ms=1000,
        )
    )
    store.append(
        RewardEvent(
            episode_id="ep1",
            source=RewardSource.SUBTASK_THUMB,
            value=1.0,
            recorded_at_ms=2000,
        )
    )
    assert store.effective_reward("ep1") == pytest.approx(1.18)
    # Both events queryable for audit.
    events = store.events_for("ep1")
    assert len(events) == 2
    assert {e.source for e in events} == {
        RewardSource.CRITIC_FALLBACK,
        RewardSource.SUBTASK_THUMB,
    }


def test_events_for_unknown_episode_returns_empty() -> None:
    store = EpisodeRewardsStore()
    assert store.events_for("ghost") == []


def test_events_for_returns_in_chronological_order() -> None:
    store = EpisodeRewardsStore()
    store.append(
        RewardEvent(
            episode_id="ep1",
            source=RewardSource.SUBTASK_THUMB,
            value=1.0,
            recorded_at_ms=2000,
        )
    )
    store.append(
        RewardEvent(
            episode_id="ep1",
            source=RewardSource.CRITIC_FALLBACK,
            value=0.18,
            recorded_at_ms=1000,
        )
    )
    events = store.events_for("ep1")
    # Sorted ascending by recorded_at_ms.
    assert [e.recorded_at_ms for e in events] == [1000, 2000]
