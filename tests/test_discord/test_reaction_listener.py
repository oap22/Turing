"""ReactionListener: Discord 👍/👎 → reward events (issue #120)."""

from __future__ import annotations

import pytest

from turing.coordinator.discord_surfaces import DiscordSurfaceIndex
from turing.coordinator.episode_rewards import EpisodeRewardsStore, RewardSource
from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.discord_bot.reaction_listener import (
    THUMBS_DOWN,
    THUMBS_UP,
    ReactionListener,
)

OPERATOR = "user-op"


def _episode(
    *,
    subtask_id: str,
    specialty: str,
    output_key: str = "",
    consumed_keys: tuple[str, ...] = (),
) -> Episode:
    return Episode(
        task_id="t-1",
        subtask_id=subtask_id,
        worker_id="w",
        specialty=specialty,
        model_version="m",
        adapter_version="a",
        input_text="i",
        trajectory=("s",),
        output_text="o",
        success=True,
        latency_ms=1,
        tokens_used=1,
        outcome=SubtaskState.COMPLETED,
        critic_score=0.0,
        recorded_at_ms=0,
        output_key=output_key,
        consumed_keys=consumed_keys,
    )


def _listener(
    *,
    index: DiscordSurfaceIndex,
    rewards: EpisodeRewardsStore,
    synthesis: Episode | None = None,
    upstreams: list[Episode] | None = None,
) -> ReactionListener:
    upstreams = upstreams or []

    def lookup(_task_id: str):
        return synthesis, list(upstreams)

    return ReactionListener(
        index=index,
        rewards=rewards,
        task_episodes_lookup=lookup,
        operator_user_id=OPERATOR,
        now_ms=lambda: 1_000,
    )


# ── Subtask thumbs ───────────────────────────────────────────────────


def test_thumb_up_on_subtask_thread_writes_subtask_thumb_plus_1():
    idx = DiscordSurfaceIndex()
    idx.record_subtask_thread("th-1", "st-1", 0)
    rewards = EpisodeRewardsStore()
    listener = _listener(index=idx, rewards=rewards)
    listener.on_reaction_add(message_id="m-99", thread_id="th-1", emoji=THUMBS_UP, user_id=OPERATOR)
    assert rewards.effective_reward("st-1") == pytest.approx(1.0)
    ev = rewards.events_for("st-1")[0]
    assert ev.source is RewardSource.SUBTASK_THUMB
    assert ev.discord_user_id == OPERATOR
    assert ev.discord_message_id == "m-99"


def test_thumb_down_on_subtask_thread_writes_negative():
    idx = DiscordSurfaceIndex()
    idx.record_subtask_thread("th-1", "st-1", 0)
    rewards = EpisodeRewardsStore()
    listener = _listener(index=idx, rewards=rewards)
    listener.on_reaction_add(
        message_id="m-1", thread_id="th-1", emoji=THUMBS_DOWN, user_id=OPERATOR
    )
    assert rewards.effective_reward("st-1") == pytest.approx(-1.0)


# ── Synthesis thumbs ─────────────────────────────────────────────────


def test_thumb_up_on_live_dag_message_credits_synthesis_and_consumed_upstreams():
    idx = DiscordSurfaceIndex()
    idx.record_task_message("m-main", "t-1", 0)
    rewards = EpisodeRewardsStore()
    syn = _episode(
        subtask_id="syn",
        specialty="synthesis",
        output_key="syn-out",
        consumed_keys=("k-r1",),
    )
    upstreams = [
        _episode(subtask_id="r1", specialty="research-deep", output_key="k-r1"),
        _episode(subtask_id="r2", specialty="research-deep", output_key="k-r2"),
    ]
    listener = _listener(index=idx, rewards=rewards, synthesis=syn, upstreams=upstreams)
    listener.on_reaction_add(message_id="m-main", thread_id=None, emoji=THUMBS_UP, user_id=OPERATOR)
    assert rewards.effective_reward("syn") == pytest.approx(1.0)
    assert rewards.effective_reward("r1") == pytest.approx(0.3)
    assert rewards.effective_reward("r2") == pytest.approx(0.0)


# ── Filter cases ─────────────────────────────────────────────────────


def test_unmapped_message_is_ignored():
    idx = DiscordSurfaceIndex()
    rewards = EpisodeRewardsStore()
    listener = _listener(index=idx, rewards=rewards)
    listener.on_reaction_add(
        message_id="unknown", thread_id="unknown-thread", emoji=THUMBS_UP, user_id=OPERATOR
    )
    assert rewards.events_for("anything") == []


def test_non_operator_reactions_are_ignored():
    idx = DiscordSurfaceIndex()
    idx.record_subtask_thread("th-1", "st-1", 0)
    rewards = EpisodeRewardsStore()
    listener = _listener(index=idx, rewards=rewards)
    listener.on_reaction_add(
        message_id="m-1", thread_id="th-1", emoji=THUMBS_UP, user_id="someone-else"
    )
    assert rewards.effective_reward("st-1") == pytest.approx(0.0)


def test_non_thumb_emoji_ignored():
    idx = DiscordSurfaceIndex()
    idx.record_subtask_thread("th-1", "st-1", 0)
    rewards = EpisodeRewardsStore()
    listener = _listener(index=idx, rewards=rewards)
    listener.on_reaction_add(message_id="m-1", thread_id="th-1", emoji="🚀", user_id=OPERATOR)
    assert rewards.effective_reward("st-1") == pytest.approx(0.0)


# ── Removal ──────────────────────────────────────────────────────────


def test_reaction_remove_after_thumb_up_zeroes_reward():
    idx = DiscordSurfaceIndex()
    idx.record_subtask_thread("th-1", "st-1", 0)
    rewards = EpisodeRewardsStore()
    listener = _listener(index=idx, rewards=rewards)
    listener.on_reaction_add(message_id="m-1", thread_id="th-1", emoji=THUMBS_UP, user_id=OPERATOR)
    listener.on_reaction_remove(
        message_id="m-1", thread_id="th-1", emoji=THUMBS_UP, user_id=OPERATOR
    )
    assert rewards.effective_reward("st-1") == pytest.approx(0.0)


def test_reaction_remove_for_synthesis_zeroes_synthesis_reward():
    idx = DiscordSurfaceIndex()
    idx.record_task_message("m-main", "t-1", 0)
    rewards = EpisodeRewardsStore()
    syn = _episode(subtask_id="syn", specialty="synthesis", output_key="o", consumed_keys=())
    listener = _listener(index=idx, rewards=rewards, synthesis=syn)
    listener.on_reaction_add(message_id="m-main", thread_id=None, emoji=THUMBS_UP, user_id=OPERATOR)
    listener.on_reaction_remove(
        message_id="m-main", thread_id=None, emoji=THUMBS_UP, user_id=OPERATOR
    )
    assert rewards.effective_reward("syn") == pytest.approx(0.0)


# ── Idempotency ──────────────────────────────────────────────────────


def test_duplicate_thumb_up_does_not_double_record():
    idx = DiscordSurfaceIndex()
    idx.record_subtask_thread("th-1", "st-1", 0)
    rewards = EpisodeRewardsStore()
    listener = _listener(index=idx, rewards=rewards)
    listener.on_reaction_add(message_id="m-1", thread_id="th-1", emoji=THUMBS_UP, user_id=OPERATOR)
    listener.on_reaction_add(message_id="m-1", thread_id="th-1", emoji=THUMBS_UP, user_id=OPERATOR)
    assert rewards.effective_reward("st-1") == pytest.approx(1.0)
    # Only one event recorded.
    same_user_msg = [
        e
        for e in rewards.events_for("st-1")
        if e.discord_user_id == OPERATOR and e.discord_message_id == "m-1"
    ]
    assert len(same_user_msg) == 1
