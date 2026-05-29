"""Reward-magnitude parity: webui curation vs the retired Discord surface.

ADR 0010 §1 requires reward attribution be preserved **verbatim** from the
retired Discord surface; only the input affordance changes. These tests pin
the webui ``QueueManager`` emitter against the *exact* magnitudes the Discord
path used, by comparing the rows it writes to ``episode_rewards`` against the
canonical writers (``write_morning_curation``, ``write_synthesis_thumb``).

If a future change drifts a magnitude, these fail — the reward pipeline is the
load-bearing path the Discord bot deletion (Slice D) is gated on.
"""

from __future__ import annotations

from dataclasses import dataclass

from turing.coordinator.episode_rewards import (
    EpisodeRewardsStore,
    RewardSource,
    write_synthesis_thumb,
)
from turing.coordinator.flywheel.morning_curation import (
    ACCEPT_REWARD,
    EDIT_REWARD,
    REJECT_REWARD,
)
from turing.gateway.queue_manager import QueueItem, QueueManager


@dataclass(frozen=True)
class _FakeEpisode:
    """Minimal stand-in for lifecycle.Episode for write_synthesis_thumb."""

    subtask_id: str
    output_key: str = ""
    consumed_keys: tuple[str, ...] = ()


def _const_clock(t: int = 5_000):
    return lambda: t


def _manager(rewards: EpisodeRewardsStore) -> QueueManager:
    return QueueManager(rewards=rewards, broadcast=None, now_ms=_const_clock())


async def _curate(decision: str, *, episode_id: str, upstreams=()) -> EpisodeRewardsStore:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards)
    await mgr.add(QueueItem(id="q1", prompt="p", specialty="s"))
    await mgr.draft("q1", episode_id=episode_id, consumed_upstreams=upstreams)
    if decision == "accept":
        await mgr.accept("q1")
    elif decision == "reject":
        await mgr.reject("q1")
    elif decision == "edit":
        await mgr.edit("q1", corrected_answer="corrected")
    return rewards


# ── Curation magnitudes match morning_curation's constants exactly ───────────


async def test_accept_writes_plus_one_morning_curation() -> None:
    rewards = await _curate("accept", episode_id="ep1")
    events = rewards.events_for("ep1")
    assert len(events) == 1
    assert events[0].source is RewardSource.MORNING_CURATION
    assert events[0].value == ACCEPT_REWARD == 1.0


async def test_reject_writes_minus_one_morning_curation() -> None:
    rewards = await _curate("reject", episode_id="ep1")
    events = rewards.events_for("ep1")
    assert len(events) == 1
    assert events[0].source is RewardSource.MORNING_CURATION
    assert events[0].value == REJECT_REWARD == -1.0


async def test_edit_writes_fractional_morning_curation() -> None:
    rewards = await _curate("edit", episode_id="ep1")
    events = rewards.events_for("ep1")
    assert len(events) == 1
    assert events[0].source is RewardSource.MORNING_CURATION
    assert events[0].value == EDIT_REWARD == 0.3


# ── Synthesis-attributed fractional credit matches write_synthesis_thumb ─────


async def test_accept_synthesis_credits_consumed_upstreams_plus_point_three() -> None:
    """Accept on a synthesis draft mirrors a positive synthesis thumb's
    fractional credit (+0.3) to each consumed upstream."""
    # Webui path: accept a synthesis draft that consumed two upstreams.
    webui = await _curate("accept", episode_id="syn", upstreams=("up-a", "up-b"))

    # Discord path: a positive synthesis thumb over the same topology.
    discord = EpisodeRewardsStore()
    write_synthesis_thumb(
        discord,
        synthesis_episode=_FakeEpisode("syn", consumed_keys=("ka", "kb")),
        upstream_episodes=[
            _FakeEpisode("up-a", output_key="ka"),
            _FakeEpisode("up-b", output_key="kb"),
        ],
        positive=True,
        recorded_at_ms=5_000,
    )

    # Both fan +0.3 to each upstream.
    assert webui.effective_reward("up-a") == 0.3
    assert webui.effective_reward("up-b") == 0.3
    assert discord.effective_reward("up-a") == 0.3
    assert discord.effective_reward("up-b") == 0.3
    # The upstream credit rows are the same source on both surfaces.
    for store in (webui, discord):
        ev = store.events_for("up-a")[0]
        assert ev.source is RewardSource.SYNTHESIS_THUMB_FRACTIONAL
        assert ev.value == 0.3


async def test_reject_synthesis_credits_consumed_upstreams_minus_point_three() -> None:
    """Reject mirrors a negative synthesis thumb: −0.3 to each consumed upstream."""
    webui = await _curate("reject", episode_id="syn", upstreams=("up-a",))
    discord = EpisodeRewardsStore()
    write_synthesis_thumb(
        discord,
        synthesis_episode=_FakeEpisode("syn", consumed_keys=("ka",)),
        upstream_episodes=[_FakeEpisode("up-a", output_key="ka")],
        positive=False,
        recorded_at_ms=5_000,
    )
    assert webui.effective_reward("up-a") == -0.3
    assert discord.effective_reward("up-a") == -0.3
    assert webui.events_for("up-a")[0].source is RewardSource.SYNTHESIS_THUMB_FRACTIONAL


async def test_edit_does_not_fan_synthesis_credit() -> None:
    """Edit is the operator's own answer, not an endorsement of upstreams —
    so it credits only the synthesis episode, never the upstreams."""
    rewards = await _curate("edit", episode_id="syn", upstreams=("up-a", "up-b"))
    assert rewards.effective_reward("syn") == 0.3
    assert rewards.events_for("up-a") == []
    assert rewards.events_for("up-b") == []
