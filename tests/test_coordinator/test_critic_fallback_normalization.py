"""critic_fallback normalization per ADR 0006 §4.

The mapping ``(critic_score - 0.5) * 0.6`` puts critic-fallback values in
``[-0.3, +0.3]`` — same magnitude as synthesis-attributed credit, weaker
than a direct thumb. Matches the PRD's "thumb is highest weight" intent.
"""

from __future__ import annotations

import pytest

from turing.coordinator.episode_rewards import (
    EpisodeRewardsStore,
    RewardSource,
    critic_fallback_value,
    write_critic_fallback,
)


def test_neutral_critic_score_maps_to_zero() -> None:
    assert critic_fallback_value(0.5) == pytest.approx(0.0)


def test_max_critic_score_maps_to_plus_three_tenths() -> None:
    assert critic_fallback_value(1.0) == pytest.approx(0.3)


def test_min_critic_score_maps_to_minus_three_tenths() -> None:
    assert critic_fallback_value(0.0) == pytest.approx(-0.3)


def test_high_critic_score_falls_inside_bounds() -> None:
    """A high but not-perfect score (0.8) lands at +0.18 per the example
    in ADR 0006 §4."""
    assert critic_fallback_value(0.8) == pytest.approx(0.18)


def test_write_critic_fallback_inserts_event_with_correct_source() -> None:
    store = EpisodeRewardsStore()
    write_critic_fallback(store, episode_id="ep1", critic_score=0.8, recorded_at_ms=1000)
    events = store.events_for("ep1")
    assert len(events) == 1
    assert events[0].source is RewardSource.CRITIC_FALLBACK
    assert events[0].value == pytest.approx(0.18)
    assert events[0].discord_user_id is None
    assert events[0].discord_message_id is None
