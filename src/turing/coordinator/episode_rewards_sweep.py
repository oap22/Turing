"""Nightly 02:00 critic-fallback reward sweep (issue #123, ADR 0006 §4).

For every closed-≥24h episode that the operator never thumbed and that
has a usable critic score, fire a single CRITIC_FALLBACK reward event so
the corpus builder isn't stranded with zero-signal rows. A late thumb
arriving after the fallback fires stacks naturally — `effective_reward`
is the SUM of all events for that episode.

The sweep is idempotent: pre-existing thumb rows or a previously-written
fallback row both short-circuit, so re-running on the same store inserts
nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turing.coordinator.episode_rewards import (
    RewardSource,
    write_critic_fallback,
)
from turing.coordinator.lifecycle.episode_store import CriticStatus
from turing.coordinator.lifecycle.lifecycle import SubtaskState

if TYPE_CHECKING:
    from turing.coordinator.episode_rewards import EpisodeRewardsStore
    from turing.coordinator.lifecycle.episode_store import EpisodeStore


REWARD_FALLBACK_AGE_MS = 24 * 60 * 60 * 1000

_THUMB_SOURCES = frozenset({RewardSource.SUBTASK_THUMB, RewardSource.SYNTHESIS_THUMB_FRACTIONAL})


def sweep_critic_fallbacks(
    *,
    episode_store: EpisodeStore,
    rewards_store: EpisodeRewardsStore,
    now_ms: int,
    enabled: bool = True,
) -> int:
    """Run one pass of the fallback sweep. Returns the number of rows written.

    `enabled=False` (i.e. `TURING_REWARD_FALLBACK_ENABLED=false`) makes the
    function a no-op. The episode/store iteration is still skipped, so the
    sweep is cheap to leave on the schedule even when the flag is off.
    """
    if not enabled:
        return 0

    cutoff = now_ms - REWARD_FALLBACK_AGE_MS
    written = 0
    for episode in episode_store.all_episodes():
        if episode.recorded_at_ms > cutoff:
            continue  # younger than 24h
        if episode.outcome is SubtaskState.REJECTED:
            continue
        status = episode_store.critic_status_of(episode.subtask_id)
        if status is not CriticStatus.SCORED:
            continue
        if _has_existing_reward(rewards_store, episode.subtask_id):
            continue
        write_critic_fallback(
            rewards_store,
            episode_id=episode.subtask_id,
            critic_score=episode.critic_score,
            recorded_at_ms=now_ms,
        )
        written += 1
    return written


def _has_existing_reward(rewards_store: EpisodeRewardsStore, episode_id: str) -> bool:
    for event in rewards_store.events_for(episode_id):
        if event.source is RewardSource.CRITIC_FALLBACK:
            return True
        if event.source in _THUMB_SOURCES:
            return True
    return False
