"""48h reconnect reconcile-cancel backfill (issue #122, ADR 0006 §6).

When the Discord bot connects (or reconnects), some thumb add/remove events
may have happened during the gap. This sweep walks every surface posted
within the 48h horizon, asks Discord for the current 👍/👎 reaction set, and
diffs against `EpisodeRewardsStore`:

  * Operator currently reacts but the store has no positive net for that
    `(episode, user, message)` triple → insert the missing thumb.
  * Store has a positive net but the operator no longer reacts → call
    `cancel_reaction` to negate the prior thumb.

Idempotency: re-running the sweep on the same Discord state writes nothing
on the second pass — the diff goes empty after the first reconciliation.

The Discord client is injected as a thin Protocol so tests pass a fake
returning canned reaction lists. Production wires the bot's existing
rate-limit-aware client and respects 429 backoff there (the sweep itself
is pure logic over what the client returns).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from turing.coordinator.episode_rewards import (
    cancel_reaction,
    write_subtask_thumb,
    write_synthesis_thumb,
)
from turing.discord_bot.reaction_listener import THUMBS_DOWN, THUMBS_UP

if TYPE_CHECKING:
    from turing.coordinator.discord_surfaces import DiscordSurfaceIndex
    from turing.coordinator.episode_rewards import EpisodeRewardsStore
    from turing.coordinator.lifecycle.episode_store import Episode


RECONCILE_HORIZON_MS = 48 * 60 * 60 * 1000


class _ReactionsClient(Protocol):
    async def get_reactors(self, *, message_id: str, emoji: str) -> list[str]: ...


# Lookup of synthesis + upstream episodes for a task_id (matches G2's signature).
TaskEpisodesLookup = Callable[[str], "tuple[Episode | None, list[Episode]]"]


def _net_for(rewards_store: EpisodeRewardsStore, *, episode_id: str, user: str, message: str) -> float:
    return sum(
        e.value
        for e in rewards_store.events_for(episode_id)
        if e.discord_user_id == user and e.discord_message_id == message
    )


async def reconcile_reactions(
    *,
    discord_client: _ReactionsClient,
    index: DiscordSurfaceIndex,
    rewards: EpisodeRewardsStore,
    task_episodes_lookup: TaskEpisodesLookup,
    operator_user_id: str,
    now_ms: int,
    horizon_ms: int = RECONCILE_HORIZON_MS,
) -> int:
    """Run one reconcile pass. Returns the number of rows written (insert + cancel)."""
    rows = 0

    # Subtask threads first.
    for thread_id, subtask_id in index.subtask_threads_within(
        now_ms=now_ms, horizon_ms=horizon_ms
    ):
        rows += await _reconcile_subtask_surface(
            discord_client=discord_client,
            rewards=rewards,
            episode_id=subtask_id,
            message_id=thread_id,
            operator=operator_user_id,
            now_ms=now_ms,
        )

    # Live-DAG main messages.
    for message_id, task_id in index.task_messages_within(
        now_ms=now_ms, horizon_ms=horizon_ms
    ):
        synthesis, upstreams = task_episodes_lookup(task_id)
        if synthesis is None:
            continue
        rows += await _reconcile_synthesis_surface(
            discord_client=discord_client,
            rewards=rewards,
            synthesis=synthesis,
            upstreams=upstreams,
            message_id=message_id,
            operator=operator_user_id,
            now_ms=now_ms,
        )

    return rows


async def _reconcile_subtask_surface(
    *,
    discord_client: _ReactionsClient,
    rewards: EpisodeRewardsStore,
    episode_id: str,
    message_id: str,
    operator: str,
    now_ms: int,
) -> int:
    rows = 0
    up = await discord_client.get_reactors(message_id=message_id, emoji=THUMBS_UP)
    down = await discord_client.get_reactors(message_id=message_id, emoji=THUMBS_DOWN)

    operator_up = operator in up
    operator_down = operator in down
    net = _net_for(rewards, episode_id=episode_id, user=operator, message=message_id)

    if operator_up and net <= 0:
        write_subtask_thumb(
            rewards,
            episode_id=episode_id,
            positive=True,
            recorded_at_ms=now_ms,
            discord_user_id=operator,
            discord_message_id=message_id,
        )
        rows += 1
    elif operator_down and net >= 0 and not operator_up:
        write_subtask_thumb(
            rewards,
            episode_id=episode_id,
            positive=False,
            recorded_at_ms=now_ms,
            discord_user_id=operator,
            discord_message_id=message_id,
        )
        rows += 1
    elif not operator_up and not operator_down and net != 0:
        cancel_reaction(
            rewards,
            episode_id=episode_id,
            discord_user_id=operator,
            discord_message_id=message_id,
            recorded_at_ms=now_ms,
        )
        rows += 1
    return rows


async def _reconcile_synthesis_surface(
    *,
    discord_client: _ReactionsClient,
    rewards: EpisodeRewardsStore,
    synthesis: Episode,
    upstreams: list[Episode],
    message_id: str,
    operator: str,
    now_ms: int,
) -> int:
    rows = 0
    up = await discord_client.get_reactors(message_id=message_id, emoji=THUMBS_UP)
    down = await discord_client.get_reactors(message_id=message_id, emoji=THUMBS_DOWN)

    operator_up = operator in up
    operator_down = operator in down
    net = _net_for(
        rewards, episode_id=synthesis.subtask_id, user=operator, message=message_id
    )

    if operator_up and net <= 0:
        write_synthesis_thumb(
            rewards,
            synthesis_episode=synthesis,
            upstream_episodes=upstreams,
            positive=True,
            recorded_at_ms=now_ms,
            discord_user_id=operator,
            discord_message_id=message_id,
        )
        rows += 1
    elif operator_down and net >= 0 and not operator_up:
        write_synthesis_thumb(
            rewards,
            synthesis_episode=synthesis,
            upstream_episodes=upstreams,
            positive=False,
            recorded_at_ms=now_ms,
            discord_user_id=operator,
            discord_message_id=message_id,
        )
        rows += 1
    elif not operator_up and not operator_down and net != 0:
        cancel_reaction(
            rewards,
            episode_id=synthesis.subtask_id,
            discord_user_id=operator,
            discord_message_id=message_id,
            recorded_at_ms=now_ms,
        )
        rows += 1
    return rows
