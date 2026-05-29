"""QueueManager — five-stage frontier projection + reward emitter (ADR 0010 Slice C).

Covers the state machine (add → approve → dispatch → draft → curate), the
``queue.snapshot`` / ``queue.delta`` frame fan-out, and idempotency. Reward
*magnitude* parity with the retired Discord surface lives in
``test_queue_reward_parity.py``; this file pins the projection mechanics.
"""

from __future__ import annotations

from turing.coordinator.episode_rewards import EpisodeRewardsStore, RewardSource
from turing.coordinator.flywheel.morning_curation import CurationDecision
from turing.gateway.queue_manager import (
    QueueItem,
    QueueItemNotFoundError,
    QueueManager,
    QueueStatus,
)


class _Clock:
    """Monotone fake clock so timestamps are deterministic and ordered."""

    def __init__(self) -> None:
        self._t = 1_000

    def __call__(self) -> int:
        self._t += 1
        return self._t


def _manager(frames: list[dict] | None = None) -> QueueManager:
    sink = frames if frames is not None else []

    async def _broadcast(frame: dict) -> None:
        sink.append(frame)

    return QueueManager(
        rewards=EpisodeRewardsStore(),
        broadcast=_broadcast,
        now_ms=_Clock(),
    )


def _item(item_id: str = "q1", **kw) -> QueueItem:
    base = {"id": item_id, "prompt": "why is the sky blue?", "specialty": "physics"}
    base.update(kw)
    return QueueItem(**base)


async def test_add_creates_proposed_item_and_broadcasts_add_delta() -> None:
    frames: list[dict] = []
    mgr = _manager(frames)
    item = await mgr.add(_item())
    assert item.status is QueueStatus.PROPOSED
    assert len(mgr) == 1
    assert frames[-1]["type"] == "queue.delta"
    assert frames[-1]["action"] == "add"
    assert frames[-1]["item"]["id"] == "q1"
    assert frames[-1]["item"]["status"] == "proposed"


async def test_add_is_idempotent_no_second_frame() -> None:
    frames: list[dict] = []
    mgr = _manager(frames)
    await mgr.add(_item())
    await mgr.add(_item(prompt="ignored second write"))
    assert len(mgr) == 1
    # First write wins; only one add frame emitted.
    assert sum(1 for f in frames if f["action"] == "add") == 1
    assert mgr.get("q1").prompt == "why is the sky blue?"


async def test_full_pipeline_transitions_stamp_timestamps() -> None:
    mgr = _manager()
    await mgr.add(_item())
    approved = await mgr.approve("q1")
    assert approved.status is QueueStatus.APPROVED
    assert approved.approved_at_ms is not None

    inflight = await mgr.dispatch("q1")
    assert inflight.status is QueueStatus.IN_FLIGHT
    assert inflight.dispatched_at_ms is not None

    drafted = await mgr.draft("q1", episode_id="ep-q1")
    assert drafted.status is QueueStatus.DRAFTED
    assert drafted.drafted_at_ms is not None
    assert drafted.episode_id == "ep-q1"

    accepted = await mgr.accept("q1")
    assert accepted.status is QueueStatus.CURATED
    assert accepted.curated_at_ms is not None
    assert accepted.decision is CurationDecision.ACCEPT


async def test_each_transition_emits_named_delta() -> None:
    frames: list[dict] = []
    mgr = _manager(frames)
    await mgr.add(_item())
    await mgr.approve("q1")
    await mgr.dispatch("q1")
    await mgr.draft("q1", episode_id="ep-q1")
    await mgr.reject("q1")
    actions = [f["action"] for f in frames if f["type"] == "queue.delta"]
    assert actions == ["add", "approve", "dispatch", "draft", "reject"]


async def test_snapshot_frame_serializes_all_items_sorted() -> None:
    mgr = _manager()
    await mgr.add(_item("q1", created_at_ms=10))
    await mgr.add(_item("q2", created_at_ms=5))
    frame = mgr.snapshot_frame()
    assert frame["type"] == "queue.snapshot"
    # Oldest-first by created_at_ms.
    assert [i["id"] for i in frame["items"]] == ["q2", "q1"]
    assert "timestamp_ms" in frame


async def test_unknown_item_raises_not_found() -> None:
    mgr = _manager()
    for op in (mgr.approve, mgr.accept, mgr.reject):
        try:
            await op("ghost")
        except QueueItemNotFoundError:
            pass
        else:  # pragma: no cover - failure path
            raise AssertionError(f"{op.__name__} did not raise for unknown item")


async def test_by_status_filters() -> None:
    mgr = _manager()
    await mgr.add(_item("q1"))
    await mgr.add(_item("q2"))
    await mgr.approve("q1")
    assert [i.id for i in mgr.by_status(QueueStatus.PROPOSED)] == ["q2"]
    assert [i.id for i in mgr.by_status(QueueStatus.APPROVED)] == ["q1"]


async def test_edit_captures_corrected_answer_and_partial_reward() -> None:
    rewards = EpisodeRewardsStore()
    mgr = QueueManager(rewards=rewards, broadcast=None, now_ms=_Clock())
    await mgr.add(_item("q1"))
    await mgr.draft("q1", episode_id="ep-q1")
    edited = await mgr.edit("q1", corrected_answer="Rayleigh scattering.")
    assert edited.decision is CurationDecision.EDIT
    assert edited.corrected_answer == "Rayleigh scattering."
    events = rewards.events_for("ep-q1")
    assert len(events) == 1
    assert events[0].source is RewardSource.MORNING_CURATION
    assert events[0].value == 0.3


async def test_idempotent_curation_decision_does_not_double_write() -> None:
    rewards = EpisodeRewardsStore()
    mgr = QueueManager(rewards=rewards, broadcast=None, now_ms=_Clock())
    await mgr.add(_item("q1"))
    await mgr.draft("q1", episode_id="ep-q1")
    await mgr.accept("q1")
    # A redelivered accept (and a conflicting reject) on an already-curated item
    # is a no-op: neither the reward nor the decision changes.
    again = await mgr.accept("q1")
    conflicting = await mgr.reject("q1")
    assert again.decision is CurationDecision.ACCEPT
    assert conflicting.decision is CurationDecision.ACCEPT
    events = rewards.events_for("ep-q1")
    assert len(events) == 1
    assert events[0].value == 1.0


async def test_reject_without_draft_writes_no_reward() -> None:
    """Rejecting a never-run proposed item is valid and emits no reward row."""
    rewards = EpisodeRewardsStore()
    mgr = QueueManager(rewards=rewards, broadcast=None, now_ms=_Clock())
    await mgr.add(_item("q1"))  # episode_id is None — never dispatched
    rejected = await mgr.reject("q1")
    assert rejected.status is QueueStatus.CURATED
    assert rejected.decision is CurationDecision.REJECT
    # No episode → no reward target → no rows written anywhere.
    assert rewards.events_for("ep-q1") == []


async def test_no_broadcast_is_safe() -> None:
    """A manager built without a broadcaster mutates state without erroring."""
    mgr = QueueManager(rewards=EpisodeRewardsStore(), broadcast=None, now_ms=_Clock())
    await mgr.add(_item("q1"))
    await mgr.approve("q1")
    assert mgr.get("q1").status is QueueStatus.APPROVED
