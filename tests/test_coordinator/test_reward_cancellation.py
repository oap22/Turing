"""Reward cancellation rows per ADR 0006 §6.

When the operator un-reacts during a bot-offline window, on reconnect
the reconcile sweep emits a *cancellation row* with ``value`` equal to
the negation of the prior reaction's value, same ``discord_user_id``
and ``discord_message_id``. Sum stays self-consistent (B2 additive
semantics); audit history preserved.
"""

from __future__ import annotations

import pytest

from turing.coordinator.episode_rewards import (
    EpisodeRewardsStore,
    RewardEvent,
    RewardSource,
    cancel_reaction,
    write_subtask_thumb,
)


def test_cancellation_zeros_out_prior_thumb() -> None:
    store = EpisodeRewardsStore()
    write_subtask_thumb(
        store,
        episode_id="ep1",
        positive=True,
        recorded_at_ms=1000,
        discord_user_id="user-1",
        discord_message_id="msg-9",
    )
    cancel_reaction(
        store,
        episode_id="ep1",
        discord_user_id="user-1",
        discord_message_id="msg-9",
        recorded_at_ms=2000,
    )
    assert store.effective_reward("ep1") == pytest.approx(0.0)


def test_cancellation_preserves_audit_history() -> None:
    store = EpisodeRewardsStore()
    write_subtask_thumb(
        store,
        episode_id="ep1",
        positive=True,
        recorded_at_ms=1000,
        discord_user_id="user-1",
        discord_message_id="msg-9",
    )
    cancel_reaction(
        store,
        episode_id="ep1",
        discord_user_id="user-1",
        discord_message_id="msg-9",
        recorded_at_ms=2000,
    )
    events = store.events_for("ep1")
    assert len(events) == 2  # both rows present
    assert events[0].value == pytest.approx(1.0)
    assert events[1].value == pytest.approx(-1.0)
    assert events[1].source is RewardSource.SUBTASK_THUMB  # source unchanged


def test_cancellation_only_targets_matching_user_and_message() -> None:
    """If two operators thumbed the same episode, cancelling one user's
    reaction must not zero out the other's."""
    store = EpisodeRewardsStore()
    # Operator A thumbs up.
    store.append(
        RewardEvent(
            episode_id="ep1",
            source=RewardSource.SUBTASK_THUMB,
            value=1.0,
            recorded_at_ms=1000,
            discord_user_id="user-A",
            discord_message_id="msg-9",
        )
    )
    # Operator B thumbs up the same surface.
    store.append(
        RewardEvent(
            episode_id="ep1",
            source=RewardSource.SUBTASK_THUMB,
            value=1.0,
            recorded_at_ms=1100,
            discord_user_id="user-B",
            discord_message_id="msg-9",
        )
    )
    # Reconnect: A retracted, B is still active. Cancel A only.
    cancel_reaction(
        store,
        episode_id="ep1",
        discord_user_id="user-A",
        discord_message_id="msg-9",
        recorded_at_ms=2000,
    )
    # Effective: A's +1 - 1 + B's +1 = +1.
    assert store.effective_reward("ep1") == pytest.approx(1.0)


def test_cancel_with_no_matching_prior_is_noop() -> None:
    """Cancelling a (user, message) that wasn't recorded does nothing —
    the reconcile sweep is allowed to be over-eager without corrupting
    state."""
    store = EpisodeRewardsStore()
    cancel_reaction(
        store,
        episode_id="ep1",
        discord_user_id="user-1",
        discord_message_id="msg-9",
        recorded_at_ms=2000,
    )
    assert store.events_for("ep1") == []
    assert store.effective_reward("ep1") == pytest.approx(0.0)
