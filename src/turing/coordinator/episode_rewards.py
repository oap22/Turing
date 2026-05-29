"""EpisodeRewardsStore — event-table reward model per ADR 0006.

Rewards are *events*, not a single per-episode column. The schema:

    episode_id, source, value, recorded_at_ms,
    discord_user_id, discord_message_id

Multiple rows per episode are expected — direct subtask thumb plus
synthesis-attributed fractional credit plus critic-fallback all stack on
the same episode. Effective reward = ``SUM(value)``.

A cancellation row (operator un-reacting during a bot-offline window)
is just an additional event whose ``value`` is the negation of the
prior reaction's value, with the same ``discord_user_id`` and
``discord_message_id``. Audit history preserved; sum stays
self-consistent.

This deep module owns the *reward state* — the small surface
(``append``, ``effective_reward``, ``events_for``) hides the storage,
the ordering, and the source-enum machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from turing.coordinator.lifecycle.episode_store import Episode

SUBTASK_THUMB_VALUE = 1.0
SYNTHESIS_THUMB_VALUE = 1.0
SYNTHESIS_FRACTIONAL_CREDIT = 0.3


class RewardSource(str, Enum):  # noqa: UP042
    """Where a reward event came from."""

    SUBTASK_THUMB = "subtask_thumb"
    SYNTHESIS_THUMB_FRACTIONAL = "synthesis_thumb_fractional"
    CRITIC_FALLBACK = "critic_fallback"
    MORNING_CURATION = "morning_curation"


@dataclass(frozen=True)
class RewardEvent:
    episode_id: str
    source: RewardSource
    value: float
    recorded_at_ms: int
    discord_user_id: str | None = None
    discord_message_id: str | None = None


class EpisodeRewardsStore:
    def __init__(self) -> None:
        self._events: dict[str, list[RewardEvent]] = {}

    def append(self, event: RewardEvent) -> None:
        self._events.setdefault(event.episode_id, []).append(event)

    def effective_reward(self, episode_id: str) -> float:
        return sum(e.value for e in self._events.get(episode_id, []))

    def events_for(self, episode_id: str) -> list[RewardEvent]:
        return sorted(
            self._events.get(episode_id, ()),
            key=lambda e: e.recorded_at_ms,
        )


def critic_fallback_value(critic_score: float) -> float:
    """Map a critic_score in [0, 1] to a fallback reward in [-0.3, +0.3].

    Per ADR 0006 §4: ``(critic_score - 0.5) * 0.6``. Critic fallback is
    weaker than a direct thumb (which is ±1.0); same magnitude as
    synthesis-attributed credit (±0.3). "Thumbs are the trunk; critic
    fallback is a perturbation."
    """
    return (critic_score - 0.5) * 0.6


def write_subtask_thumb(
    store: EpisodeRewardsStore,
    *,
    episode_id: str,
    positive: bool,
    recorded_at_ms: int,
    discord_user_id: str | None = None,
    discord_message_id: str | None = None,
) -> None:
    """Append a SUBTASK_THUMB reward event for a thread-thumbed subtask."""
    sign = 1.0 if positive else -1.0
    store.append(
        RewardEvent(
            episode_id=episode_id,
            source=RewardSource.SUBTASK_THUMB,
            value=sign * SUBTASK_THUMB_VALUE,
            recorded_at_ms=recorded_at_ms,
            discord_user_id=discord_user_id,
            discord_message_id=discord_message_id,
        )
    )


def write_synthesis_thumb(
    store: EpisodeRewardsStore,
    *,
    synthesis_episode: Episode,
    upstream_episodes: Iterable[Episode],
    positive: bool,
    recorded_at_ms: int,
    discord_user_id: str | None = None,
    discord_message_id: str | None = None,
) -> None:
    """Append synthesis-thumb reward events per ADR 0006 §3.

    The synthesis episode itself gets ±1.0; each upstream subtask whose
    ``output_key`` appears in ``synthesis_episode.consumed_keys`` gets
    ±0.3 (synthesis attribution restricted to consumed upstreams).
    Upstream subtasks the synthesis didn't read are *not* credited.
    """
    sign = 1.0 if positive else -1.0
    consumed = set(synthesis_episode.consumed_keys)
    store.append(
        RewardEvent(
            episode_id=synthesis_episode.subtask_id,
            source=RewardSource.SYNTHESIS_THUMB_FRACTIONAL,
            value=sign * SYNTHESIS_THUMB_VALUE,
            recorded_at_ms=recorded_at_ms,
            discord_user_id=discord_user_id,
            discord_message_id=discord_message_id,
        )
    )
    for upstream in upstream_episodes:
        if upstream.output_key and upstream.output_key in consumed:
            store.append(
                RewardEvent(
                    episode_id=upstream.subtask_id,
                    source=RewardSource.SYNTHESIS_THUMB_FRACTIONAL,
                    value=sign * SYNTHESIS_FRACTIONAL_CREDIT,
                    recorded_at_ms=recorded_at_ms,
                    discord_user_id=discord_user_id,
                    discord_message_id=discord_message_id,
                )
            )


def cancel_reaction(
    store: EpisodeRewardsStore,
    *,
    episode_id: str,
    discord_user_id: str,
    discord_message_id: str,
    recorded_at_ms: int,
) -> None:
    """Append a cancellation row negating the latest matching reaction.

    Per ADR 0006 §6: bot-reconnect reconcile when the operator un-reacted
    during the gap. Matches on (episode_id, discord_user_id,
    discord_message_id). Aggregates the matching reactions' net value
    (positive thumbs added, prior cancellations subtracted) and writes
    a single row with the negated total. If the net is already zero, the
    cancel is a no-op — re-running the reconcile sweep is idempotent.
    """
    matching = [
        e
        for e in store.events_for(episode_id)
        if e.discord_user_id == discord_user_id and e.discord_message_id == discord_message_id
    ]
    if not matching:
        return
    net = sum(e.value for e in matching)
    if net == 0:
        return
    # Use the source of the most-recent matching event so audit history
    # tells you "this was a cancellation of a SUBTASK_THUMB event."
    source = matching[-1].source
    store.append(
        RewardEvent(
            episode_id=episode_id,
            source=source,
            value=-net,
            recorded_at_ms=recorded_at_ms,
            discord_user_id=discord_user_id,
            discord_message_id=discord_message_id,
        )
    )


def write_critic_fallback(
    store: EpisodeRewardsStore,
    *,
    episode_id: str,
    critic_score: float,
    recorded_at_ms: int,
) -> None:
    """Append a CRITIC_FALLBACK reward event for ``episode_id``.

    The 24-hour-no-feedback sweep (nightly job) is the typical caller.
    """
    store.append(
        RewardEvent(
            episode_id=episode_id,
            source=RewardSource.CRITIC_FALLBACK,
            value=critic_fallback_value(critic_score),
            recorded_at_ms=recorded_at_ms,
        )
    )


def write_morning_curation(
    store: EpisodeRewardsStore,
    *,
    episode_id: str,
    value: float,
    recorded_at_ms: int,
) -> None:
    """Append a MORNING_CURATION reward event for ``episode_id``.

    In Phase 0 the operator's morning accept / reject / edit decision *is* the
    reward signal (ADR 0009 supersedes the ADR 0004 real-time critic and the
    ADR 0006 Discord reward UI for Phase 0). The caller maps the decision to a
    ``value`` (e.g. accept → +1.0, reject → −1.0, edit → a partial credit).
    """
    store.append(
        RewardEvent(
            episode_id=episode_id,
            source=RewardSource.MORNING_CURATION,
            value=value,
            recorded_at_ms=recorded_at_ms,
        )
    )
