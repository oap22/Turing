"""Discord reaction → reward event listener (issue #120, ADR 0006 §5).

Single operator assumption: only `operator_user_id`'s reactions count;
all other users are ignored at the entry point. Only 👍 / 👎 are recognized.

Routing:
  - `message_id` in DiscordSurfaceIndex.task_messages → live-DAG main message
    → write_synthesis_thumb against the task's synthesis episode + upstreams.
  - `thread_id` in DiscordSurfaceIndex.subtask_threads → subtask thread
    → write_subtask_thumb against the subtask episode.
  - Anything else → ignore.

Idempotency: before adding a thumb event, scan the existing events for the
same (episode, user, message) triple. If their net value already matches
the sign we'd add, skip — prevents duplicate-add events from doubling the
recorded score.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from turing.coordinator.episode_rewards import (
    cancel_reaction,
    write_subtask_thumb,
    write_synthesis_thumb,
)

if TYPE_CHECKING:
    from turing.coordinator.discord_surfaces import DiscordSurfaceIndex
    from turing.coordinator.episode_rewards import EpisodeRewardsStore
    from turing.coordinator.lifecycle.episode_store import Episode

THUMBS_UP = "👍"
THUMBS_DOWN = "👎"

# Lookup returns (synthesis_episode_or_None, upstream_episodes) for a task_id.
TaskEpisodesLookup = Callable[[str], "tuple[Episode | None, list[Episode]]"]


class ReactionListener:
    def __init__(
        self,
        *,
        index: DiscordSurfaceIndex,
        rewards: EpisodeRewardsStore,
        task_episodes_lookup: TaskEpisodesLookup,
        operator_user_id: str,
        now_ms: Callable[[], int],
    ) -> None:
        self._index = index
        self._rewards = rewards
        self._task_episodes_lookup = task_episodes_lookup
        self._operator = operator_user_id
        self._now_ms = now_ms

    # ── Public entry points ──────────────────────────────────────────

    def on_reaction_add(
        self,
        *,
        message_id: str,
        thread_id: str | None,
        emoji: str,
        user_id: str,
    ) -> None:
        if not self._is_recognized(user_id, emoji):
            return
        positive = emoji == THUMBS_UP

        task_id = self._index.task_for_message(message_id)
        if task_id is not None:
            self._handle_synthesis_add(
                task_id=task_id,
                message_id=message_id,
                user_id=user_id,
                positive=positive,
            )
            return

        if thread_id is not None:
            subtask_id = self._index.subtask_for_thread(thread_id)
            if subtask_id is not None:
                self._handle_subtask_add(
                    subtask_id=subtask_id,
                    message_id=message_id,
                    user_id=user_id,
                    positive=positive,
                )

    def on_reaction_remove(
        self,
        *,
        message_id: str,
        thread_id: str | None,
        emoji: str,
        user_id: str,
    ) -> None:
        if not self._is_recognized(user_id, emoji):
            return
        task_id = self._index.task_for_message(message_id)
        if task_id is not None:
            synthesis, _ = self._task_episodes_lookup(task_id)
            if synthesis is None:
                return
            cancel_reaction(
                self._rewards,
                episode_id=synthesis.subtask_id,
                discord_user_id=user_id,
                discord_message_id=message_id,
                recorded_at_ms=self._now_ms(),
            )
            return
        if thread_id is not None:
            subtask_id = self._index.subtask_for_thread(thread_id)
            if subtask_id is not None:
                cancel_reaction(
                    self._rewards,
                    episode_id=subtask_id,
                    discord_user_id=user_id,
                    discord_message_id=message_id,
                    recorded_at_ms=self._now_ms(),
                )

    # ── Internals ────────────────────────────────────────────────────

    def _is_recognized(self, user_id: str, emoji: str) -> bool:
        if user_id != self._operator:
            return False
        return emoji in (THUMBS_UP, THUMBS_DOWN)

    def _already_recorded(
        self, *, episode_id: str, user_id: str, message_id: str, positive: bool
    ) -> bool:
        net = sum(
            e.value
            for e in self._rewards.events_for(episode_id)
            if e.discord_user_id == user_id and e.discord_message_id == message_id
        )
        if net == 0:
            return False
        return (net > 0) is positive

    def _handle_subtask_add(
        self, *, subtask_id: str, message_id: str, user_id: str, positive: bool
    ) -> None:
        if self._already_recorded(
            episode_id=subtask_id,
            user_id=user_id,
            message_id=message_id,
            positive=positive,
        ):
            return
        write_subtask_thumb(
            self._rewards,
            episode_id=subtask_id,
            positive=positive,
            recorded_at_ms=self._now_ms(),
            discord_user_id=user_id,
            discord_message_id=message_id,
        )

    def _handle_synthesis_add(
        self, *, task_id: str, message_id: str, user_id: str, positive: bool
    ) -> None:
        synthesis, upstreams = self._task_episodes_lookup(task_id)
        if synthesis is None:
            return
        if self._already_recorded(
            episode_id=synthesis.subtask_id,
            user_id=user_id,
            message_id=message_id,
            positive=positive,
        ):
            return
        write_synthesis_thumb(
            self._rewards,
            synthesis_episode=synthesis,
            upstream_episodes=upstreams,
            positive=positive,
            recorded_at_ms=self._now_ms(),
            discord_user_id=user_id,
            discord_message_id=message_id,
        )
