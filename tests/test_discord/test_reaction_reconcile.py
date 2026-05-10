"""48h reconnect reconcile-cancel backfill (issue #122)."""

from __future__ import annotations

import pytest

from turing.coordinator.discord_surfaces import DiscordSurfaceIndex
from turing.coordinator.episode_rewards import (
    EpisodeRewardsStore,
    write_subtask_thumb,
)
from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.discord_bot.reaction_listener import THUMBS_DOWN, THUMBS_UP
from turing.discord_bot.reaction_reconcile import (
    RECONCILE_HORIZON_MS,
    reconcile_reactions,
)

OPERATOR = "user-op"


class _FakeDiscord:
    """Canned reactor lists keyed by (message_id, emoji)."""

    def __init__(self, table: dict[tuple[str, str], list[str]]) -> None:
        self._table = table
        self.calls: list[tuple[str, str]] = []

    async def get_reactors(self, *, message_id: str, emoji: str) -> list[str]:
        self.calls.append((message_id, emoji))
        return list(self._table.get((message_id, emoji), []))


def _episode(
    *, subtask_id: str, output_key: str = "", consumed_keys: tuple[str, ...] = ()
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
        success=True,
        latency_ms=1,
        tokens_used=1,
        outcome=SubtaskState.COMPLETED,
        critic_score=0.0,
        recorded_at_ms=0,
        output_key=output_key,
        consumed_keys=consumed_keys,
    )


def _index_with_subtask(
    thread_id: str, subtask_id: str, posted_at_ms: int = 0
) -> DiscordSurfaceIndex:
    idx = DiscordSurfaceIndex()
    idx.record_subtask_thread(thread_id, subtask_id, posted_at_ms)
    return idx


# ── Insert path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insert_missing_thumb_up():
    idx = _index_with_subtask("th-1", "s-1")
    discord = _FakeDiscord({("th-1", THUMBS_UP): [OPERATOR], ("th-1", THUMBS_DOWN): []})
    rewards = EpisodeRewardsStore()

    rows = await reconcile_reactions(
        discord_client=discord,
        index=idx,
        rewards=rewards,
        task_episodes_lookup=lambda _t: (None, []),
        operator_user_id=OPERATOR,
        now_ms=10,
    )
    assert rows == 1
    assert rewards.effective_reward("s-1") == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_insert_missing_thumb_down():
    idx = _index_with_subtask("th-1", "s-1")
    discord = _FakeDiscord({("th-1", THUMBS_UP): [], ("th-1", THUMBS_DOWN): [OPERATOR]})
    rewards = EpisodeRewardsStore()

    rows = await reconcile_reactions(
        discord_client=discord,
        index=idx,
        rewards=rewards,
        task_episodes_lookup=lambda _t: (None, []),
        operator_user_id=OPERATOR,
        now_ms=10,
    )
    assert rows == 1
    assert rewards.effective_reward("s-1") == pytest.approx(-1.0)


# ── Cancel path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_when_reward_present_but_reaction_absent():
    idx = _index_with_subtask("th-1", "s-1")
    rewards = EpisodeRewardsStore()
    write_subtask_thumb(
        rewards,
        episode_id="s-1",
        positive=True,
        recorded_at_ms=1,
        discord_user_id=OPERATOR,
        discord_message_id="th-1",
    )
    discord = _FakeDiscord({})  # No reactions present anywhere.

    rows = await reconcile_reactions(
        discord_client=discord,
        index=idx,
        rewards=rewards,
        task_episodes_lookup=lambda _t: (None, []),
        operator_user_id=OPERATOR,
        now_ms=10,
    )
    assert rows == 1
    assert rewards.effective_reward("s-1") == pytest.approx(0.0)


# ── Idempotency ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_pass_writes_nothing():
    idx = _index_with_subtask("th-1", "s-1")
    discord = _FakeDiscord({("th-1", THUMBS_UP): [OPERATOR], ("th-1", THUMBS_DOWN): []})
    rewards = EpisodeRewardsStore()

    first = await reconcile_reactions(
        discord_client=discord,
        index=idx,
        rewards=rewards,
        task_episodes_lookup=lambda _t: (None, []),
        operator_user_id=OPERATOR,
        now_ms=10,
    )
    second = await reconcile_reactions(
        discord_client=discord,
        index=idx,
        rewards=rewards,
        task_episodes_lookup=lambda _t: (None, []),
        operator_user_id=OPERATOR,
        now_ms=20,
    )
    assert first == 1
    assert second == 0
    assert rewards.effective_reward("s-1") == pytest.approx(1.0)


# ── 48h horizon ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_surfaces_outside_48h_horizon_are_skipped():
    idx = DiscordSurfaceIndex()
    now = 100_000_000
    idx.record_subtask_thread("th-old", "s-old", now - RECONCILE_HORIZON_MS - 10)
    idx.record_subtask_thread("th-new", "s-new", now - 1_000)

    discord = _FakeDiscord(
        {
            ("th-old", THUMBS_UP): [OPERATOR],
            ("th-new", THUMBS_UP): [OPERATOR],
        }
    )
    rewards = EpisodeRewardsStore()

    await reconcile_reactions(
        discord_client=discord,
        index=idx,
        rewards=rewards,
        task_episodes_lookup=lambda _t: (None, []),
        operator_user_id=OPERATOR,
        now_ms=now,
    )
    # Old thread skipped; new one reconciled.
    assert rewards.effective_reward("s-old") == pytest.approx(0.0)
    assert rewards.effective_reward("s-new") == pytest.approx(1.0)
    # The fake client was only asked about the in-horizon thread.
    asked_messages = {m for m, _e in discord.calls}
    assert asked_messages == {"th-new"}


# ── Synthesis surface ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesis_surface_insert_thumb_up():
    idx = DiscordSurfaceIndex()
    idx.record_task_message("m-main", "t-1", 0)
    discord = _FakeDiscord({("m-main", THUMBS_UP): [OPERATOR], ("m-main", THUMBS_DOWN): []})
    rewards = EpisodeRewardsStore()

    syn = _episode(
        subtask_id="syn",
        output_key="syn-out",
        consumed_keys=("k-r1",),
    )
    upstreams = [
        _episode(subtask_id="r1", output_key="k-r1"),
    ]
    await reconcile_reactions(
        discord_client=discord,
        index=idx,
        rewards=rewards,
        task_episodes_lookup=lambda _t: (syn, upstreams),
        operator_user_id=OPERATOR,
        now_ms=10,
    )
    assert rewards.effective_reward("syn") == pytest.approx(1.0)
    assert rewards.effective_reward("r1") == pytest.approx(0.3)


# ── Non-operator reactions ignored ───────────────────────────────────


@pytest.mark.asyncio
async def test_non_operator_reactor_does_not_trigger_insert():
    idx = _index_with_subtask("th-1", "s-1")
    discord = _FakeDiscord({("th-1", THUMBS_UP): ["someone-else"], ("th-1", THUMBS_DOWN): []})
    rewards = EpisodeRewardsStore()

    rows = await reconcile_reactions(
        discord_client=discord,
        index=idx,
        rewards=rewards,
        task_episodes_lookup=lambda _t: (None, []),
        operator_user_id=OPERATOR,
        now_ms=10,
    )
    assert rows == 0
    assert rewards.effective_reward("s-1") == pytest.approx(0.0)
