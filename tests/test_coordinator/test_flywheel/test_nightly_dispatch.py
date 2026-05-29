"""Tests for the Phase 0 nightly dispatch (issue #261, ADR 0009 §2 "Night").

Exercises the human-gated question queue + NightlyDispatcher against an
InMemoryBus with stub workers, mirroring tests/test_coordinator/test_dispatch.
"""

from __future__ import annotations

import json

import pytest

from turing.coordinator.dispatch import (
    SubtaskDispatchClient,
    SubtaskTimeoutError,
    TaskResult,
)
from turing.coordinator.flywheel import (
    NightlyDispatcher,
    QuestionQueue,
    ResearchQuestion,
)
from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.registry import CapabilityRegistry
from turing.coordinator.registry.manifest import (
    CURRENT_MANIFEST_VERSION,
    CapabilityManifest,
)
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner

SPECIALTY = "ai-ml-generalist"


def _now_ms_factory(start: int = 1_700_000_000_000):
    counter = {"t": start}

    def now() -> int:
        counter["t"] += 1
        return counter["t"]

    return now


@pytest.fixture()
def transports():
    bus = InMemoryBus()
    coord_signer = MessageSigner.generate()
    worker_signer = MessageSigner.generate()
    now = _now_ms_factory()
    coord = SignedTransport(
        bus=bus, signer=coord_signer, trusted_keys=[worker_signer.public_key], now_ms=now
    )
    worker = SignedTransport(
        bus=bus, signer=worker_signer, trusted_keys=[coord_signer.public_key], now_ms=now
    )
    return bus, coord, worker


def _manifest(worker_id: str) -> CapabilityManifest:
    return CapabilityManifest(
        worker_id=worker_id,
        specialties=(SPECIALTY,),
        base_model="qwen2.5:7b",
        adapters=(),
        tools=("vault_query", "web_fetch"),
        hardware="jetson-orin-nano-super",
        max_concurrent=1,
        eval_score=0.5,
        public_key=b"\x01" * 32,
        schema_version=CURRENT_MANIFEST_VERSION,
    )


def _registry(*worker_ids: str) -> CapabilityRegistry:
    reg = CapabilityRegistry(now_ms=lambda: 0, heartbeat_ttl_ms=10_000)
    for wid in worker_ids:
        reg.register(_manifest(wid))
    return reg


async def _direct_worker(
    transport: SignedTransport, worker_id: str, *, captured: list[str] | None = None
) -> None:
    """A worker subscribing only to its direct subject, echoing a result."""

    async def handle(msg: MeshMessage) -> None:
        payload = json.loads(msg.payload.decode("utf-8"))
        if captured is not None:
            captured.append(payload["subtask_id"])
        result = TaskResult(
            subtask_id=payload["subtask_id"],
            worker_id=worker_id,
            status="COMPLETED",
            output=f"answer-from-{worker_id}",
            tokens_used=12,
            latency_ms=7,
            model="qwen2.5:7b",
        )
        reply = MeshMessage(
            request_id=f"r-{payload['subtask_id']}",
            sender_id=worker_id,
            subject=f"subtasks.{payload['subtask_id']}.result",
            payload=json.dumps(result.to_dict()).encode("utf-8"),
            timestamp_ms=1_700_000_000_500,  # within the transport replay window
        )
        await transport.publish(reply)

    await transport.subscribe(f"subtasks.workers.{worker_id}", handle)


def _dispatcher(coord, queue, episodes, registry, now):
    client = SubtaskDispatchClient(transport=coord, sender_id="coordinator", now_ms=now)
    return NightlyDispatcher(
        queue=queue,
        dispatch_client=client,
        episode_store=episodes,
        registry=registry,
        now_ms=now,
        deadline_ms=10_000,
    )


@pytest.mark.asyncio
async def test_only_approved_questions_are_dispatched(transports) -> None:
    _, coord, worker = transports
    captured: list[str] = []
    await _direct_worker(worker, "w1", captured=captured)

    queue = QuestionQueue()
    queue.add(ResearchQuestion("q-approved", "What is LoRA?", SPECIALTY, created_at_ms=1))
    queue.add(ResearchQuestion("q-pending", "What is DPO?", SPECIALTY, created_at_ms=2))
    queue.approve("q-approved")  # q-pending stays unapproved

    episodes = EpisodeStore()
    dispatcher = _dispatcher(coord, queue, episodes, _registry("w1"), _now_ms_factory())

    report = await dispatcher.run_nightly(batch_id="night-1")

    assert captured == ["q-approved"]
    assert report.dispatched == ("q-approved",)
    assert report.succeeded == ("q-approved",)
    # The un-approved question was never sent and is still runnable-pending.
    assert queue.get("q-pending").dispatched_at_ms is None


@pytest.mark.asyncio
async def test_questions_fan_out_evenly_across_workers(transports) -> None:
    _, coord, worker = transports
    seen: dict[str, list[str]] = {"w1": [], "w2": [], "w3": [], "w4": []}
    for wid in seen:
        # each worker records the subtasks it personally received
        await _direct_worker(worker, wid, captured=seen[wid])

    queue = QuestionQueue()
    for i in range(8):
        qid = f"q{i}"
        queue.add(ResearchQuestion(qid, f"question {i}", SPECIALTY, created_at_ms=i))
        queue.approve(qid)

    episodes = EpisodeStore()
    registry = _registry("w1", "w2", "w3", "w4")
    dispatcher = _dispatcher(coord, queue, episodes, registry, _now_ms_factory())

    report = await dispatcher.run_nightly()

    assert len(report.dispatched) == 8
    # 8 questions, 4 workers, round-robin → exactly 2 each.
    assert all(len(got) == 2 for got in seen.values()), seen


@pytest.mark.asyncio
async def test_each_closed_subtask_writes_an_episode(transports) -> None:
    _, coord, worker = transports
    await _direct_worker(worker, "w1")

    queue = QuestionQueue()
    queue.add(ResearchQuestion("q1", "Explain attention.", SPECIALTY, created_at_ms=1))
    queue.approve("q1")

    episodes = EpisodeStore()
    dispatcher = _dispatcher(coord, queue, episodes, _registry("w1"), _now_ms_factory())

    await dispatcher.run_nightly(batch_id="batch-7")

    ep = episodes.get("q1")
    assert ep.task_id == "batch-7"
    assert ep.specialty == SPECIALTY
    assert ep.worker_id == "w1"
    assert ep.output_text == "answer-from-w1"
    assert ep.outcome is SubtaskState.COMPLETED
    assert ep.success is True
    assert queue.get("q1").dispatched_at_ms is not None


class _TimeoutClient:
    """Stub dispatch client whose every dispatch times out.

    Drives the dispatcher's timeout path deterministically — relying on a real
    0.05s wall-clock bus timeout is fragile under a loaded event loop when other
    async tests run first.
    """

    now_ms = staticmethod(lambda: 0)

    async def dispatch(self, envelope, *, worker_id=None, deadline_ms, grace_s=30.0):
        raise SubtaskTimeoutError("forced timeout for test")


@pytest.mark.asyncio
async def test_timeout_records_failed_episode_and_marks_dispatched() -> None:
    queue = QuestionQueue()
    queue.add(ResearchQuestion("q-dead", "unanswerable in time", SPECIALTY, created_at_ms=1))
    queue.approve("q-dead")

    episodes = EpisodeStore()
    dispatcher = NightlyDispatcher(
        queue=queue,
        dispatch_client=_TimeoutClient(),
        episode_store=episodes,
        registry=_registry("w1"),
        now_ms=_now_ms_factory(),
        deadline_ms=0,
        grace_s=0.05,
    )

    report = await dispatcher.run_nightly()

    assert report.failed == ("q-dead",)
    assert report.succeeded == ()
    ep = episodes.get("q-dead")
    assert ep.outcome is SubtaskState.TIMED_OUT
    assert ep.success is False
    assert queue.get("q-dead").dispatched_at_ms is not None


@pytest.mark.asyncio
async def test_question_with_no_live_worker_is_skipped(transports) -> None:
    _, coord, _worker = transports
    queue = QuestionQueue()
    queue.add(ResearchQuestion("q-orphan", "no worker for this", "code-review", created_at_ms=1))
    queue.approve("q-orphan")

    episodes = EpisodeStore()
    # registry only knows ai-ml-generalist workers, not code-review.
    dispatcher = _dispatcher(coord, queue, episodes, _registry("w1"), _now_ms_factory())

    report = await dispatcher.run_nightly()

    assert report.skipped == ("q-orphan",)
    assert report.dispatched == ()
    # Skipped → left runnable for the next night.
    assert queue.get("q-orphan").is_runnable
