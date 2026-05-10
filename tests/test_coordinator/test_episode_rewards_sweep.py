"""Nightly critic-fallback reward sweep (issue #123)."""

from __future__ import annotations

import pytest

from turing.coordinator.episode_rewards import (
    EpisodeRewardsStore,
    RewardSource,
    write_subtask_thumb,
)
from turing.coordinator.episode_rewards_sweep import (
    REWARD_FALLBACK_AGE_MS,
    sweep_critic_fallbacks,
)
from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState

NOW = 1_700_000_000_000


def _episode(
    *,
    subtask_id: str,
    age_ms: int,
    critic_score: float = 0.0,
    outcome: SubtaskState = SubtaskState.COMPLETED,
) -> Episode:
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
        critic_score=critic_score,
        recorded_at_ms=NOW - age_ms,
    )


def _store_with_scored(
    *, subtask_id: str, age_ms: int, score: float, outcome=SubtaskState.COMPLETED
):
    eps = EpisodeStore()
    eps.record(_episode(subtask_id=subtask_id, age_ms=age_ms, critic_score=score, outcome=outcome))
    if outcome is not SubtaskState.REJECTED:
        eps.update_critic_score(subtask_id=subtask_id, critic_score=score)
    return eps


# ── Eligibility ───────────────────────────────────────────────────────


def test_25h_old_scored_episode_with_no_thumbs_writes_fallback():
    eps = _store_with_scored(subtask_id="s-1", age_ms=25 * 60 * 60 * 1000, score=0.8)
    rew = EpisodeRewardsStore()
    written = sweep_critic_fallbacks(episode_store=eps, rewards_store=rew, now_ms=NOW)
    assert written == 1
    events = rew.events_for("s-1")
    assert len(events) == 1
    ev = events[0]
    assert ev.source is RewardSource.CRITIC_FALLBACK
    # value = (critic_score - 0.5) * 0.6 per #111 helper
    assert ev.value == pytest.approx((0.8 - 0.5) * 0.6)


def test_23h_old_scored_episode_is_skipped():
    eps = _store_with_scored(subtask_id="s-1", age_ms=23 * 60 * 60 * 1000, score=0.8)
    rew = EpisodeRewardsStore()
    written = sweep_critic_fallbacks(episode_store=eps, rewards_store=rew, now_ms=NOW)
    assert written == 0
    assert rew.events_for("s-1") == []


def test_pre_existing_subtask_thumb_blocks_fallback():
    eps = _store_with_scored(subtask_id="s-1", age_ms=25 * 60 * 60 * 1000, score=0.8)
    rew = EpisodeRewardsStore()
    write_subtask_thumb(
        rew,
        episode_id="s-1",
        positive=True,
        recorded_at_ms=NOW - 12 * 60 * 60 * 1000,
    )
    written = sweep_critic_fallbacks(episode_store=eps, rewards_store=rew, now_ms=NOW)
    assert written == 0
    sources = {e.source for e in rew.events_for("s-1")}
    assert RewardSource.CRITIC_FALLBACK not in sources


def test_idempotent_on_second_run():
    eps = _store_with_scored(subtask_id="s-1", age_ms=25 * 60 * 60 * 1000, score=0.8)
    rew = EpisodeRewardsStore()
    sweep_critic_fallbacks(episode_store=eps, rewards_store=rew, now_ms=NOW)
    second = sweep_critic_fallbacks(episode_store=eps, rewards_store=rew, now_ms=NOW)
    assert second == 0
    assert len(rew.events_for("s-1")) == 1


def test_rejected_episode_skipped():
    # Outcome REJECTED — record_at age old, but should never get a fallback.
    eps = EpisodeStore()
    eps.record(
        _episode(
            subtask_id="s-1",
            age_ms=25 * 60 * 60 * 1000,
            critic_score=0.9,
            outcome=SubtaskState.REJECTED,
        )
    )
    # Note: critic_status stays PENDING for REJECTED rows; the outcome guard
    # is what matters here.
    rew = EpisodeRewardsStore()
    written = sweep_critic_fallbacks(episode_store=eps, rewards_store=rew, now_ms=NOW)
    assert written == 0


def test_critic_status_failed_skipped():
    eps = EpisodeStore()
    eps.record(_episode(subtask_id="s-1", age_ms=25 * 60 * 60 * 1000, critic_score=0.0))
    eps.mark_critic_failed(subtask_id="s-1")
    rew = EpisodeRewardsStore()
    written = sweep_critic_fallbacks(episode_store=eps, rewards_store=rew, now_ms=NOW)
    assert written == 0


def test_pending_critic_status_skipped():
    # Closed long ago but never scored → no critic_score to fall back to.
    eps = EpisodeStore()
    eps.record(_episode(subtask_id="s-1", age_ms=25 * 60 * 60 * 1000, critic_score=0.0))
    rew = EpisodeRewardsStore()
    written = sweep_critic_fallbacks(episode_store=eps, rewards_store=rew, now_ms=NOW)
    assert written == 0


# ── Late-thumb stacking ──────────────────────────────────────────────


def test_late_thumb_stacks_with_fallback():
    eps = _store_with_scored(subtask_id="s-1", age_ms=25 * 60 * 60 * 1000, score=0.8)
    rew = EpisodeRewardsStore()
    sweep_critic_fallbacks(episode_store=eps, rewards_store=rew, now_ms=NOW)
    fallback_value = (0.8 - 0.5) * 0.6
    assert rew.effective_reward("s-1") == pytest.approx(fallback_value)

    # Late thumb arrives; should stack, not replace.
    write_subtask_thumb(rew, episode_id="s-1", positive=True, recorded_at_ms=NOW + 1)
    assert rew.effective_reward("s-1") == pytest.approx(fallback_value + 1.0)


# ── Config flag ──────────────────────────────────────────────────────


def test_disabled_flag_is_noop():
    eps = _store_with_scored(subtask_id="s-1", age_ms=25 * 60 * 60 * 1000, score=0.8)
    rew = EpisodeRewardsStore()
    written = sweep_critic_fallbacks(
        episode_store=eps, rewards_store=rew, now_ms=NOW, enabled=False
    )
    assert written == 0
    assert rew.events_for("s-1") == []


def test_age_constant_matches_24h():
    assert REWARD_FALLBACK_AGE_MS == 24 * 60 * 60 * 1000
