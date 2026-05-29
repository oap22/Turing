"""ChatManager — ad-hoc chat-thread projection + reward delegation (ADR 0010 Slice E).

Covers the session/subtask state machine (submit → plan → stream → complete →
thumb), the ``chat.snapshot`` / ``chat.delta`` frame fan-out, idempotency, and
that per-subtask thumbs land rewards through the REUSED Slice C emitter. Reward
*equivalence* with the queue surface (proving "reuses C's emitter") lives in
``test_chat_reward_equivalence.py``; this file pins the projection mechanics,
mirroring ``test_queue_manager.py``.
"""

from __future__ import annotations

import pytest

from turing.coordinator.episode_rewards import EpisodeRewardsStore, RewardSource
from turing.coordinator.flywheel.morning_curation import CurationDecision
from turing.gateway.chat_manager import (
    ChatManager,
    ChatSessionNotFoundError,
    ChatSubtaskNotFoundError,
    ChatSubtaskNotRewardableError,
    ChatSubtaskStatus,
)


class _Clock:
    """Monotone fake clock so timestamps are deterministic and ordered."""

    def __init__(self) -> None:
        self._t = 1_000

    def __call__(self) -> int:
        self._t += 1
        return self._t


def _manager(
    frames: list[dict] | None = None, rewards: EpisodeRewardsStore | None = None
) -> ChatManager:
    sink = frames if frames is not None else []

    async def _broadcast(frame: dict) -> None:
        sink.append(frame)

    return ChatManager(
        rewards=rewards if rewards is not None else EpisodeRewardsStore(),
        broadcast=_broadcast if frames is not None else None,
        now_ms=_Clock(),
    )


async def _seed_completed(
    mgr: ChatManager,
    *,
    session_id: str = "s1",
    subtask_id: str = "st1",
    episode_id: str = "ep1",
    upstreams: tuple[str, ...] = (),
) -> None:
    """Drive a subtask to COMPLETED so it is thumbable."""
    await mgr.submit(session_id=session_id, prompt="do a thing")
    await mgr.plan_subtask(
        session_id,
        subtask_id=subtask_id,
        specialty="research",
        episode_id=episode_id,
        consumed_upstreams=upstreams,
    )
    await mgr.stream(session_id, subtask_id, chunk="answer", done=True)


# ── ingestion ────────────────────────────────────────────────────────────────


async def test_submit_creates_session_and_broadcasts_submit_delta() -> None:
    frames: list[dict] = []
    mgr = _manager(frames)
    session = await mgr.submit(session_id="s1", prompt="why is the sky blue?")
    assert session.prompt == "why is the sky blue?"
    assert len(mgr) == 1
    assert frames[-1]["type"] == "chat.delta"
    assert frames[-1]["action"] == "submit"
    assert frames[-1]["session"]["id"] == "s1"


async def test_submit_is_idempotent_no_second_frame() -> None:
    frames: list[dict] = []
    mgr = _manager(frames)
    await mgr.submit(session_id="s1", prompt="first")
    await mgr.submit(session_id="s1", prompt="ignored second write")
    assert len(mgr) == 1
    assert sum(1 for f in frames if f.get("action") == "submit") == 1
    assert mgr.get_session("s1").prompt == "first"


async def test_plan_subtask_appends_in_order_and_is_idempotent() -> None:
    frames: list[dict] = []
    mgr = _manager(frames)
    await mgr.submit(session_id="s1", prompt="p")
    await mgr.plan_subtask("s1", subtask_id="a", specialty="research")
    await mgr.plan_subtask("s1", subtask_id="b", specialty="physics")
    await mgr.plan_subtask("s1", subtask_id="a", specialty="ignored")  # dup
    session = mgr.get_session("s1")
    assert [s.id for s in session.subtasks] == ["a", "b"]
    assert [s.index for s in session.subtasks] == [0, 1]
    # First plan wins; only two plan frames for two distinct subtasks.
    assert sum(1 for f in frames if f.get("action") == "plan") == 2


# ── streaming ──────────────────────────────────────────────────────────────────


async def test_stream_accumulates_content_and_completes() -> None:
    frames: list[dict] = []
    mgr = _manager(frames)
    await mgr.submit(session_id="s1", prompt="p")
    await mgr.plan_subtask("s1", subtask_id="st1", specialty="research", episode_id="ep1")
    streaming = await mgr.stream("s1", "st1", chunk="par", done=False)
    assert streaming.status is ChatSubtaskStatus.STREAMING
    assert streaming.content == "par"
    completed = await mgr.stream("s1", "st1", chunk="tial", done=True)
    assert completed.status is ChatSubtaskStatus.COMPLETED
    assert completed.content == "partial"
    assert completed.completed_at_ms is not None
    actions = [f["action"] for f in frames if f["type"] == "chat.delta"]
    assert actions == ["submit", "plan", "stream", "complete"]


async def test_fail_marks_subtask_errored() -> None:
    mgr = _manager()
    await mgr.submit(session_id="s1", prompt="p")
    await mgr.plan_subtask("s1", subtask_id="st1", specialty="research", episode_id="ep1")
    failed = await mgr.fail("s1", "st1", error="worker exploded")
    assert failed.status is ChatSubtaskStatus.ERROR
    assert failed.content == "worker exploded"


# ── per-subtask thumbs write rewards via the reused emitter ────────────────────


async def test_accept_writes_plus_one_reward() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards=rewards)
    await _seed_completed(mgr, episode_id="ep1")
    accepted = await mgr.accept("s1", "st1")
    assert accepted.status is ChatSubtaskStatus.CURATED
    assert accepted.decision is CurationDecision.ACCEPT
    events = rewards.events_for("ep1")
    assert len(events) == 1
    assert events[0].source is RewardSource.MORNING_CURATION
    assert events[0].value == 1.0


async def test_reject_writes_minus_one_reward() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards=rewards)
    await _seed_completed(mgr, episode_id="ep1")
    await mgr.reject("s1", "st1")
    assert rewards.events_for("ep1")[0].value == -1.0


async def test_edit_captures_corrected_answer_and_partial_reward() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards=rewards)
    await _seed_completed(mgr, episode_id="ep1")
    edited = await mgr.edit("s1", "st1", corrected_answer="Rayleigh scattering.")
    assert edited.decision is CurationDecision.EDIT
    assert edited.corrected_answer == "Rayleigh scattering."
    events = rewards.events_for("ep1")
    assert len(events) == 1
    assert events[0].source is RewardSource.MORNING_CURATION
    assert events[0].value == 0.3


async def test_accept_synthesis_fans_fractional_credit_upstream() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards=rewards)
    await _seed_completed(mgr, episode_id="syn", upstreams=("up-a", "up-b"))
    await mgr.accept("s1", "st1")
    assert rewards.effective_reward("syn") == 1.0
    for upstream in ("up-a", "up-b"):
        ev = rewards.events_for(upstream)
        assert len(ev) == 1
        assert ev[0].source is RewardSource.SYNTHESIS_THUMB_FRACTIONAL
        assert ev[0].value == 0.3


async def test_edit_does_not_fan_synthesis_credit() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards=rewards)
    await _seed_completed(mgr, episode_id="syn", upstreams=("up-a",))
    await mgr.edit("s1", "st1", corrected_answer="corrected")
    assert rewards.effective_reward("syn") == 0.3
    assert rewards.events_for("up-a") == []


async def test_idempotent_thumb_does_not_double_write() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards=rewards)
    await _seed_completed(mgr, episode_id="ep1")
    await mgr.accept("s1", "st1")
    # A redelivered accept (and a conflicting reject) on a curated subtask is a
    # no-op: neither the reward nor the decision changes.
    again = await mgr.accept("s1", "st1")
    conflicting = await mgr.reject("s1", "st1")
    assert again.decision is CurationDecision.ACCEPT
    assert conflicting.decision is CurationDecision.ACCEPT
    events = rewards.events_for("ep1")
    assert len(events) == 1
    assert events[0].value == 1.0


async def test_thumb_on_errored_subtask_raises_and_writes_no_reward() -> None:
    """An ERROR subtask is not rewardable: every thumb raises and writes nothing."""
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards=rewards)
    await mgr.submit(session_id="s1", prompt="p")
    await mgr.plan_subtask("s1", subtask_id="st1", specialty="research", episode_id="ep1")
    await mgr.fail("s1", "st1", error="worker exploded")
    with pytest.raises(ChatSubtaskNotRewardableError):
        await mgr.accept("s1", "st1")
    with pytest.raises(ChatSubtaskNotRewardableError):
        await mgr.reject("s1", "st1")
    with pytest.raises(ChatSubtaskNotRewardableError):
        await mgr.edit("s1", "st1", corrected_answer="x")
    # The subtask stays ERROR and no reward row exists for the failed episode.
    assert mgr.get_session("s1").subtasks[0].status is ChatSubtaskStatus.ERROR
    assert rewards.events_for("ep1") == []


async def test_thumb_on_pending_subtask_raises_not_rewardable() -> None:
    """A still-streaming/never-completed subtask has no draft to reward."""
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards=rewards)
    await mgr.submit(session_id="s1", prompt="p")
    await mgr.plan_subtask("s1", subtask_id="st1", specialty="research", episode_id="ep1")
    await mgr.stream("s1", "st1", chunk="partial", done=False)  # STREAMING, not COMPLETED
    with pytest.raises(ChatSubtaskNotRewardableError):
        await mgr.accept("s1", "st1")
    assert rewards.events_for("ep1") == []


async def test_thumb_without_episode_writes_no_reward() -> None:
    """Thumbing a subtask that never produced an episode writes no reward row."""
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards=rewards)
    await mgr.submit(session_id="s1", prompt="p")
    await mgr.plan_subtask("s1", subtask_id="st1", specialty="research")  # no episode_id
    await mgr.stream("s1", "st1", chunk="x", done=True)
    rejected = await mgr.reject("s1", "st1")
    assert rejected.status is ChatSubtaskStatus.CURATED
    # No episode → no reward target → nothing written anywhere.
    assert rewards.events_for("ep1") == []


# ── frames + reads ─────────────────────────────────────────────────────────────


async def test_subtask_delta_carries_session_id_and_subtask() -> None:
    frames: list[dict] = []
    mgr = _manager(frames)
    await _seed_completed(mgr)
    await mgr.accept("s1", "st1")
    last = frames[-1]
    assert last["type"] == "chat.delta"
    assert last["action"] == "accept"
    assert last["session_id"] == "s1"
    assert last["subtask"]["id"] == "st1"
    assert last["subtask"]["status"] == "curated"


async def test_snapshot_frame_serializes_sessions_sorted() -> None:
    mgr = _manager()
    await mgr.submit(session_id="s1", prompt="first")
    await mgr.plan_subtask("s1", subtask_id="a", specialty="research")
    frame = mgr.snapshot_frame()
    assert frame["type"] == "chat.snapshot"
    assert [s["id"] for s in frame["sessions"]] == ["s1"]
    assert frame["sessions"][0]["subtasks"][0]["id"] == "a"
    assert "timestamp_ms" in frame


async def test_unknown_session_raises_not_found() -> None:
    mgr = _manager()
    for op in (mgr.accept, mgr.reject):
        try:
            await op("ghost", "st1")
        except ChatSessionNotFoundError:
            pass
        else:  # pragma: no cover - failure path
            raise AssertionError(f"{op.__name__} did not raise for unknown session")


async def test_unknown_subtask_raises_not_found() -> None:
    mgr = _manager()
    await mgr.submit(session_id="s1", prompt="p")
    try:
        await mgr.accept("s1", "ghost")
    except ChatSubtaskNotFoundError:
        pass
    else:  # pragma: no cover - failure path
        raise AssertionError("accept did not raise for unknown subtask")


async def test_no_broadcast_is_safe() -> None:
    """A manager built without a broadcaster mutates state without erroring."""
    mgr = ChatManager(rewards=EpisodeRewardsStore(), broadcast=None, now_ms=_Clock())
    await mgr.submit(session_id="s1", prompt="p")
    await mgr.plan_subtask("s1", subtask_id="st1", specialty="research", episode_id="ep1")
    assert mgr.get_session("s1").subtasks[0].id == "st1"
