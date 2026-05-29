"""Reward *equivalence*: the webui emitter writes ``episode_rewards`` rows that
are identical, for the consumer, to the retired Discord surface's.

ADR 0010 §1 promises reward attribution is preserved **verbatim** when the
operator surface moves from Discord reactions to webui buttons — "only the
input affordance changes." ``test_queue_reward_parity`` already pins the raw
*magnitudes* against ``morning_curation``'s constants. This file proves the
stronger property the Discord-bot deletion (Slice D) is gated on: for an
*equivalent operator action*, the rows the webui ``QueueManager`` lands in a
shared :class:`EpisodeRewardsStore` are byte-for-byte the same the canonical
Discord writers would have landed — judged through the lens of the load-bearing
consumer, ``effective_reward`` (the SUM the corpus builder reads).

The three reward mechanisms ADR 0010 §1 names are each exercised against their
Discord twin over the *same episode topology*:

  * **subtask thumb** (±1.0)      ── webui accept/reject of a plain subtask
                                     draft vs ``write_subtask_thumb``.
  * **synthesis + fractional**    ── webui accept/reject of a synthesis draft
    credit (±1.0 / ±0.3)            vs ``write_synthesis_thumb``.
  * **critic_fallback** (±0.3)    ── the nightly sweep stacks identically on a
                                     webui-curated episode and a Discord-thumbed
                                     one.

The one *intentional* divergence is the episode-level row's ``source`` tag —
the webui path records ``MORNING_CURATION`` (Phase-0's human critic, ADR 0009),
where Discord recorded ``SUBTASK_THUMB`` / ``SYNTHESIS_THUMB_FRACTIONAL``. That
tag is invisible to ``effective_reward`` (source-agnostic SUM); the tests assert
the *aggregate* is identical and call out the tag difference explicitly so a
future reader knows it is by design, not drift. The fan-out fractional-credit
rows to consumed upstreams ARE byte-identical (same source, same value) on both
surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass

from turing.coordinator.episode_rewards import (
    EpisodeRewardsStore,
    RewardSource,
    write_critic_fallback,
    write_subtask_thumb,
    write_synthesis_thumb,
)
from turing.gateway.queue_manager import QueueItem, QueueManager

# Both surfaces stamp the same instant so cancellation/idempotency ordering and
# any timestamp-keyed read is held constant across the comparison.
WHEN_MS = 5_000


@dataclass(frozen=True)
class _FakeEpisode:
    """Minimal lifecycle.Episode stand-in for write_synthesis_thumb."""

    subtask_id: str
    output_key: str = ""
    consumed_keys: tuple[str, ...] = ()


def _const_clock(t: int = WHEN_MS):
    return lambda: t


def _manager(rewards: EpisodeRewardsStore) -> QueueManager:
    return QueueManager(rewards=rewards, broadcast=None, now_ms=_const_clock())


async def _webui_curate(
    decision: str,
    *,
    episode_id: str,
    upstreams: tuple[str, ...] = (),
    corrected_answer: str = "corrected",
) -> EpisodeRewardsStore:
    """Drive an item all the way to a curation decision via the webui emitter."""
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards)
    await mgr.add(QueueItem(id="q1", prompt="p", specialty="s"))
    await mgr.draft("q1", episode_id=episode_id, consumed_upstreams=upstreams)
    if decision == "accept":
        await mgr.accept("q1")
    elif decision == "reject":
        await mgr.reject("q1")
    elif decision == "edit":
        await mgr.edit("q1", corrected_answer=corrected_answer)
    return rewards


def _episode_ids(store: EpisodeRewardsStore, *ids: str) -> set[str]:
    return {eid for eid in ids if store.events_for(eid)}


def _effective(store: EpisodeRewardsStore, *ids: str) -> dict[str, float]:
    """The consumer-facing reward vector: episode_id → SUM(value)."""
    return {eid: store.effective_reward(eid) for eid in ids}


# ── subtask thumb: webui accept/reject ≡ write_subtask_thumb ──────────────────


async def test_subtask_thumb_positive_equivalent_to_discord() -> None:
    """A webui *accept* on a plain subtask draft lands the same effective
    reward (+1.0) as a Discord 👍 routed through ``write_subtask_thumb``."""
    webui = await _webui_curate("accept", episode_id="st")

    discord = EpisodeRewardsStore()
    write_subtask_thumb(discord, episode_id="st", positive=True, recorded_at_ms=WHEN_MS)

    # Same episode touched, same effective reward — the corpus builder cannot
    # tell the two surfaces apart.
    assert _episode_ids(webui, "st") == _episode_ids(discord, "st") == {"st"}
    assert _effective(webui, "st") == _effective(discord, "st") == {"st": 1.0}
    # Exactly one row on each side (no stray fan-out for a plain subtask).
    assert len(webui.events_for("st")) == len(discord.events_for("st")) == 1
    # The intentional, consumer-invisible tag divergence (see module docstring).
    assert webui.events_for("st")[0].source is RewardSource.MORNING_CURATION
    assert discord.events_for("st")[0].source is RewardSource.SUBTASK_THUMB


async def test_subtask_thumb_negative_equivalent_to_discord() -> None:
    """A webui *reject* mirrors a Discord 👎: −1.0 on the subtask episode."""
    webui = await _webui_curate("reject", episode_id="st")

    discord = EpisodeRewardsStore()
    write_subtask_thumb(discord, episode_id="st", positive=False, recorded_at_ms=WHEN_MS)

    assert _effective(webui, "st") == _effective(discord, "st") == {"st": -1.0}


# ── synthesis + fractional credit: webui accept/reject ≡ write_synthesis_thumb ─


async def test_synthesis_positive_equivalent_to_discord() -> None:
    """A webui *accept* on a synthesis draft that consumed two upstreams writes
    the same effective-reward vector as a Discord synthesis 👍: +1.0 on the
    synthesis episode, +0.3 on each consumed upstream."""
    webui = await _webui_curate("accept", episode_id="syn", upstreams=("up-a", "up-b"))

    discord = EpisodeRewardsStore()
    write_synthesis_thumb(
        discord,
        synthesis_episode=_FakeEpisode("syn", consumed_keys=("ka", "kb")),
        upstream_episodes=[
            _FakeEpisode("up-a", output_key="ka"),
            _FakeEpisode("up-b", output_key="kb"),
        ],
        positive=True,
        recorded_at_ms=WHEN_MS,
    )

    # Identical set of episodes touched.
    assert (
        _episode_ids(webui, "syn", "up-a", "up-b")
        == _episode_ids(discord, "syn", "up-a", "up-b")
        == {"syn", "up-a", "up-b"}
    )
    # Identical effective-reward vector across the whole topology.
    expected = {"syn": 1.0, "up-a": 0.3, "up-b": 0.3}
    assert _effective(webui, "syn", "up-a", "up-b") == expected
    assert _effective(discord, "syn", "up-a", "up-b") == expected
    # The fractional-credit rows on consumed upstreams are byte-identical:
    # same source AND same value on both surfaces.
    for upstream in ("up-a", "up-b"):
        w = webui.events_for(upstream)
        d = discord.events_for(upstream)
        assert len(w) == len(d) == 1
        assert w[0].source is d[0].source is RewardSource.SYNTHESIS_THUMB_FRACTIONAL
        assert w[0].value == d[0].value == 0.3


async def test_synthesis_negative_equivalent_to_discord() -> None:
    """A webui *reject* mirrors a negative synthesis thumb: −1.0 / −0.3."""
    webui = await _webui_curate("reject", episode_id="syn", upstreams=("up-a",))

    discord = EpisodeRewardsStore()
    write_synthesis_thumb(
        discord,
        synthesis_episode=_FakeEpisode("syn", consumed_keys=("ka",)),
        upstream_episodes=[_FakeEpisode("up-a", output_key="ka")],
        positive=False,
        recorded_at_ms=WHEN_MS,
    )

    expected = {"syn": -1.0, "up-a": -0.3}
    assert _effective(webui, "syn", "up-a") == expected
    assert _effective(discord, "syn", "up-a") == expected
    # Negative fractional credit is the same source on both surfaces.
    assert webui.events_for("up-a")[0].source is RewardSource.SYNTHESIS_THUMB_FRACTIONAL
    assert discord.events_for("up-a")[0].source is RewardSource.SYNTHESIS_THUMB_FRACTIONAL


# ── critic_fallback: the nightly sweep stacks identically on both surfaces ─────


async def test_critic_fallback_stacks_on_webui_curated_episode_like_discord() -> None:
    """A late ``CRITIC_FALLBACK`` (±0.3) stacks on top of an operator decision
    the same way regardless of surface — ``effective_reward`` is the SUM, so a
    webui-accepted episode and a Discord-thumbed one end at the same total once
    the nightly sweep fires."""
    # critic_fallback_value(1.0) == (1.0 - 0.5) * 0.6 == 0.3.
    critic_score = 1.0

    # Webui: accept (+1.0) then a late critic fallback (+0.3) stacks.
    webui = await _webui_curate("accept", episode_id="ep")
    write_critic_fallback(webui, episode_id="ep", critic_score=critic_score, recorded_at_ms=WHEN_MS)

    # Discord: 👍 (+1.0) then the same late critic fallback (+0.3) stacks.
    discord = EpisodeRewardsStore()
    write_subtask_thumb(discord, episode_id="ep", positive=True, recorded_at_ms=WHEN_MS)
    write_critic_fallback(
        discord, episode_id="ep", critic_score=critic_score, recorded_at_ms=WHEN_MS
    )

    # Both surfaces: a positive decision + a +0.3 fallback → +1.3, two rows.
    assert _effective(webui, "ep") == _effective(discord, "ep") == {"ep": 1.3}
    assert len(webui.events_for("ep")) == len(discord.events_for("ep")) == 2
    # The fallback row itself is byte-identical (the sweep is surface-agnostic).
    w_fallback = [e for e in webui.events_for("ep") if e.source is RewardSource.CRITIC_FALLBACK]
    d_fallback = [e for e in discord.events_for("ep") if e.source is RewardSource.CRITIC_FALLBACK]
    assert len(w_fallback) == len(d_fallback) == 1
    assert w_fallback[0].value == d_fallback[0].value == 0.3


# ── idempotency parity: a redelivered decision never double-counts ────────────


async def test_repeated_webui_decision_matches_single_discord_thumb() -> None:
    """A redelivered curation POST is a no-op (the manager's ``_already_rewarded``
    guard), so the effective reward equals exactly one Discord thumb — no
    double-count, matching the Discord listener's ``_already_recorded`` guard."""
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards)
    await mgr.add(QueueItem(id="q1", prompt="p", specialty="s"))
    await mgr.draft("q1", episode_id="ep")
    await mgr.accept("q1")
    await mgr.accept("q1")  # redelivered — must not stack a second +1.0

    discord = EpisodeRewardsStore()
    write_subtask_thumb(discord, episode_id="ep", positive=True, recorded_at_ms=WHEN_MS)

    assert _effective(rewards, "ep") == _effective(discord, "ep") == {"ep": 1.0}
    assert len(rewards.events_for("ep")) == 1
