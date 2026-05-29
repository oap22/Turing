"""Reward *equivalence*: the webui CHAT surface (Slice E) writes ``episode_rewards``
rows identical, for the consumer, to the queue surface (Slice C) and the retired
Discord surface.

ADR 0010 Slice E says the chat pane "reuses the reward emitter from C". This
file proves that literally: for an *equivalent operator thumb*, the rows the
``ChatManager`` lands in a shared :class:`EpisodeRewardsStore` are byte-for-byte
the same the Slice C ``QueueManager`` lands — judged through the load-bearing
consumer ``effective_reward`` (the SUM the corpus builder reads). Because the
queue surface is itself pinned equivalent to Discord
(``test_queue_reward_equivalence``), chat ≡ queue ≡ Discord transitively.

To make "reuses C's emitter" un-fakeable, this module **REUSES the Slice C
equivalence helpers verbatim** — ``_FakeEpisode``, ``_episode_ids``,
``_effective``, ``_const_clock`` and the ``WHEN_MS`` constant are imported from
``test_queue_reward_equivalence`` rather than re-declared. The chat path is
driven to a thumb and compared against the *same* helpers the queue path is
graded with, over the *same* episode topology — so a divergence in the chat
emitter would surface as a failed equality against C's own fixtures.

The three reward mechanisms ADR 0010 §1 names are each exercised:

  * **subtask thumb** (±1.0)      ── chat accept/reject of a plain subtask.
  * **synthesis + fractional**    ── chat accept/reject of a synthesis subtask
    credit (±1.0 / ±0.3)            that consumed upstreams.
  * **critic_fallback** (±0.3)    ── the nightly sweep stacks identically on a
                                     chat-thumbed episode.
"""

from __future__ import annotations

# REUSE Slice C's equivalence helpers verbatim — importing (not re-declaring)
# them is the proof that the chat surface is graded by the exact fixtures the
# queue surface is, so "reuses C's emitter" cannot silently drift.
from tests.test_gateway.test_queue_reward_equivalence import (
    WHEN_MS,
    _const_clock,
    _effective,
    _episode_ids,
    _FakeEpisode,
)
from turing.coordinator.episode_rewards import (
    EpisodeRewardsStore,
    RewardSource,
    write_critic_fallback,
    write_subtask_thumb,
    write_synthesis_thumb,
)
from turing.gateway.chat_manager import ChatManager
from turing.gateway.queue_manager import QueueItem, QueueManager


def _chat_manager(rewards: EpisodeRewardsStore) -> ChatManager:
    return ChatManager(rewards=rewards, broadcast=None, now_ms=_const_clock())


def _queue_manager(rewards: EpisodeRewardsStore) -> QueueManager:
    return QueueManager(rewards=rewards, broadcast=None, now_ms=_const_clock())


async def _chat_thumb(
    decision: str,
    *,
    episode_id: str,
    upstreams: tuple[str, ...] = (),
    corrected_answer: str = "corrected",
) -> EpisodeRewardsStore:
    """Drive a chat subtask to a per-subtask thumb via the chat emitter."""
    rewards = EpisodeRewardsStore()
    mgr = _chat_manager(rewards)
    await mgr.submit(session_id="s1", prompt="p")
    await mgr.plan_subtask(
        "s1", subtask_id="st1", specialty="s", episode_id=episode_id, consumed_upstreams=upstreams
    )
    await mgr.stream("s1", "st1", chunk="answer", done=True)
    if decision == "accept":
        await mgr.accept("s1", "st1")
    elif decision == "reject":
        await mgr.reject("s1", "st1")
    elif decision == "edit":
        await mgr.edit("s1", "st1", corrected_answer=corrected_answer)
    return rewards


async def _queue_curate(
    decision: str, *, episode_id: str, upstreams: tuple[str, ...] = ()
) -> EpisodeRewardsStore:
    """Drive a queue item to the same curation via the Slice C emitter."""
    rewards = EpisodeRewardsStore()
    mgr = _queue_manager(rewards)
    await mgr.add(QueueItem(id="q1", prompt="p", specialty="s"))
    await mgr.draft("q1", episode_id=episode_id, consumed_upstreams=upstreams)
    if decision == "accept":
        await mgr.accept("q1")
    elif decision == "reject":
        await mgr.reject("q1")
    elif decision == "edit":
        await mgr.edit("q1", corrected_answer="corrected")
    return rewards


# ── subtask thumb: chat accept/reject ≡ queue curation ≡ Discord ──────────────


async def test_chat_subtask_thumb_positive_equivalent_to_queue_and_discord() -> None:
    """A chat *accept* on a plain subtask lands the same effective reward (+1.0)
    as a queue accept and a Discord 👍 (``write_subtask_thumb``)."""
    chat = await _chat_thumb("accept", episode_id="st")
    queue = await _queue_curate("accept", episode_id="st")

    discord = EpisodeRewardsStore()
    write_subtask_thumb(discord, episode_id="st", positive=True, recorded_at_ms=WHEN_MS)

    # Same episode touched, same effective reward across all three surfaces.
    assert (
        _episode_ids(chat, "st")
        == _episode_ids(queue, "st")
        == _episode_ids(discord, "st")
        == {"st"}
    )
    assert (
        _effective(chat, "st")
        == _effective(queue, "st")
        == _effective(discord, "st")
        == {"st": 1.0}
    )
    assert len(chat.events_for("st")) == len(queue.events_for("st")) == 1
    # Chat ≡ queue at the *row* level: same MORNING_CURATION source and value.
    chat_ev, queue_ev = chat.events_for("st")[0], queue.events_for("st")[0]
    assert chat_ev.source is queue_ev.source is RewardSource.MORNING_CURATION
    assert chat_ev.value == queue_ev.value == 1.0
    # The intentional, consumer-invisible tag divergence from Discord (it tagged
    # SUBTASK_THUMB) — same as the queue surface's documented divergence.
    assert discord.events_for("st")[0].source is RewardSource.SUBTASK_THUMB


async def test_chat_subtask_thumb_negative_equivalent_to_queue_and_discord() -> None:
    """A chat *reject* mirrors a queue reject and a Discord 👎: −1.0."""
    chat = await _chat_thumb("reject", episode_id="st")
    queue = await _queue_curate("reject", episode_id="st")

    discord = EpisodeRewardsStore()
    write_subtask_thumb(discord, episode_id="st", positive=False, recorded_at_ms=WHEN_MS)

    assert (
        _effective(chat, "st")
        == _effective(queue, "st")
        == _effective(discord, "st")
        == {"st": -1.0}
    )


# ── synthesis + fractional credit: chat ≡ queue ≡ Discord ─────────────────────


async def test_chat_synthesis_positive_equivalent_to_queue_and_discord() -> None:
    """A chat *accept* on a synthesis subtask that consumed two upstreams writes
    the same effective-reward vector as the queue surface and a Discord synthesis
    👍: +1.0 on the synthesis episode, +0.3 on each consumed upstream."""
    chat = await _chat_thumb("accept", episode_id="syn", upstreams=("up-a", "up-b"))
    queue = await _queue_curate("accept", episode_id="syn", upstreams=("up-a", "up-b"))

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

    expected = {"syn": 1.0, "up-a": 0.3, "up-b": 0.3}
    assert _effective(chat, "syn", "up-a", "up-b") == expected
    assert _effective(queue, "syn", "up-a", "up-b") == expected
    assert _effective(discord, "syn", "up-a", "up-b") == expected
    # The fractional-credit rows on consumed upstreams are byte-identical across
    # chat, queue, and Discord: same source AND same value.
    for upstream in ("up-a", "up-b"):
        c = chat.events_for(upstream)
        q = queue.events_for(upstream)
        d = discord.events_for(upstream)
        assert len(c) == len(q) == len(d) == 1
        assert c[0].source is q[0].source is d[0].source is RewardSource.SYNTHESIS_THUMB_FRACTIONAL
        assert c[0].value == q[0].value == d[0].value == 0.3


async def test_chat_synthesis_negative_equivalent_to_queue_and_discord() -> None:
    """A chat *reject* mirrors a negative synthesis thumb: −1.0 / −0.3."""
    chat = await _chat_thumb("reject", episode_id="syn", upstreams=("up-a",))
    queue = await _queue_curate("reject", episode_id="syn", upstreams=("up-a",))

    discord = EpisodeRewardsStore()
    write_synthesis_thumb(
        discord,
        synthesis_episode=_FakeEpisode("syn", consumed_keys=("ka",)),
        upstream_episodes=[_FakeEpisode("up-a", output_key="ka")],
        positive=False,
        recorded_at_ms=WHEN_MS,
    )

    expected = {"syn": -1.0, "up-a": -0.3}
    assert _effective(chat, "syn", "up-a") == expected
    assert _effective(queue, "syn", "up-a") == expected
    assert _effective(discord, "syn", "up-a") == expected
    assert chat.events_for("up-a")[0].source is RewardSource.SYNTHESIS_THUMB_FRACTIONAL


async def test_chat_edit_does_not_fan_synthesis_credit_like_queue() -> None:
    """Edit credits only the subtask episode (+0.3), never the upstreams —
    matching the queue surface exactly."""
    chat = await _chat_thumb("edit", episode_id="syn", upstreams=("up-a", "up-b"))
    queue = await _queue_curate("edit", episode_id="syn", upstreams=("up-a", "up-b"))

    assert _effective(chat, "syn") == _effective(queue, "syn") == {"syn": 0.3}
    assert chat.events_for("up-a") == queue.events_for("up-a") == []
    assert chat.events_for("up-b") == queue.events_for("up-b") == []


# ── critic_fallback: the nightly sweep stacks identically ─────────────────────


async def test_critic_fallback_stacks_on_chat_thumbed_episode_like_queue() -> None:
    """A late ``CRITIC_FALLBACK`` (±0.3) stacks on a chat thumb the same way it
    stacks on a queue curation — ``effective_reward`` is the SUM, surface-agnostic."""
    critic_score = 1.0  # critic_fallback_value(1.0) == 0.3

    chat = await _chat_thumb("accept", episode_id="ep")
    write_critic_fallback(chat, episode_id="ep", critic_score=critic_score, recorded_at_ms=WHEN_MS)

    queue = await _queue_curate("accept", episode_id="ep")
    write_critic_fallback(queue, episode_id="ep", critic_score=critic_score, recorded_at_ms=WHEN_MS)

    assert _effective(chat, "ep") == _effective(queue, "ep") == {"ep": 1.3}
    assert len(chat.events_for("ep")) == len(queue.events_for("ep")) == 2
    c_fallback = [e for e in chat.events_for("ep") if e.source is RewardSource.CRITIC_FALLBACK]
    q_fallback = [e for e in queue.events_for("ep") if e.source is RewardSource.CRITIC_FALLBACK]
    assert len(c_fallback) == len(q_fallback) == 1
    assert c_fallback[0].value == q_fallback[0].value == 0.3


# ── idempotency parity: a redelivered chat thumb never double-counts ───────────


async def test_repeated_chat_thumb_matches_single_queue_curation() -> None:
    """A redelivered chat thumb is a no-op (the manager's CURATED guard), so the
    effective reward equals exactly one queue curation — no double-count."""
    rewards = EpisodeRewardsStore()
    mgr = _chat_manager(rewards)
    await mgr.submit(session_id="s1", prompt="p")
    await mgr.plan_subtask("s1", subtask_id="st1", specialty="s", episode_id="ep")
    await mgr.stream("s1", "st1", chunk="a", done=True)
    await mgr.accept("s1", "st1")
    await mgr.accept("s1", "st1")  # redelivered — must not stack a second +1.0

    queue = await _queue_curate("accept", episode_id="ep")

    assert _effective(rewards, "ep") == _effective(queue, "ep") == {"ep": 1.0}
    assert len(rewards.events_for("ep")) == 1
