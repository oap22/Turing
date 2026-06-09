"""Tests for the frontier promote/decline bridge (ADR 0009 §2, #263).

The missing join of slice 2: a worker-proposed follow-up only becomes runnable
when a human approves it, which promotes it (with provenance) into the runnable
queue for the next night; declining records the rejection and never dispatches.
Covers the bridge in isolation plus an end-to-end propose → approve/decline →
next-night-dispatch check on the InMemoryBus harness.
"""

from __future__ import annotations

import json

import pytest

from turing.coordinator.dispatch import SubtaskDispatchClient, TaskResult
from turing.coordinator.flywheel import (
    FrontierReview,
    NightlyDispatcher,
    ProposalStatus,
    ProposedQuestion,
    ProposedQueue,
    QuestionQueue,
    promoted_question_id,
)
from turing.coordinator.flywheel.proposed_queue import proposals_to_fragment
from turing.coordinator.lifecycle.episode_store import EpisodeStore
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


def _proposal(
    pid: str = "prop-1", *, prompt: str = "What is QLoRA's NF4 dtype?"
) -> ProposedQuestion:
    return ProposedQuestion(
        proposal_id=pid,
        prompt=prompt,
        specialty=SPECIALTY,
        origin_task_id="night-0",
        origin_question_id="q-origin",
        proposed_by="jetson-1",
        created_at_ms=10,
        status=ProposalStatus.PENDING,
    )


# ── approve ──────────────────────────────────────────────────────────────────


def test_approve_promotes_into_runnable_queue_with_provenance() -> None:
    proposed = ProposedQueue()
    proposed.add(_proposal("prop-1"))
    queue = QuestionQueue()
    review = FrontierReview(proposed=proposed, queue=queue)

    question = review.approve("prop-1", now_ms=100)

    # Proposal is marked approved; a runnable question now exists.
    assert proposed.get("prop-1").status is ProposalStatus.APPROVED
    assert question.is_runnable
    assert question.origin_question_id == "q-origin"  # provenance carried
    assert question.specialty == SPECIALTY
    # It is exactly what the next nightly run would drain.
    assert queue.drain_approved() == [question]


def test_approve_is_idempotent() -> None:
    proposed = ProposedQueue()
    proposed.add(_proposal("prop-1"))
    queue = QuestionQueue()
    review = FrontierReview(proposed=proposed, queue=queue)

    q1 = review.approve("prop-1", now_ms=100)
    q2 = review.approve("prop-1", now_ms=200)

    assert q1.question_id == q2.question_id == promoted_question_id(_proposal("prop-1"))
    assert len(queue) == 1  # not duplicated


def test_approve_unknown_proposal_raises() -> None:
    review = FrontierReview(proposed=ProposedQueue(), queue=QuestionQueue())
    with pytest.raises(KeyError):
        review.approve("nope", now_ms=1)


# ── decline ──────────────────────────────────────────────────────────────────


def test_decline_marks_rejected_and_never_runnable() -> None:
    proposed = ProposedQueue()
    proposed.add(_proposal("prop-1"))
    queue = QuestionQueue()
    review = FrontierReview(proposed=proposed, queue=queue)

    review.decline("prop-1")

    assert proposed.get("prop-1").status is ProposalStatus.REJECTED
    assert len(queue) == 0
    assert queue.drain_approved() == []


# ── end-to-end: propose → review → next-night dispatch ───────────────────────


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
        bus=bus,
        signer=coord_signer,
        trusted_keys={"jetson-1": worker_signer.public_key},
        now_ms=now,
    )
    worker = SignedTransport(
        bus=bus,
        signer=worker_signer,
        trusted_keys={"coordinator": coord_signer.public_key},
        now_ms=now,
    )
    return bus, coord, worker


def _registry(worker_id: str) -> CapabilityRegistry:
    reg = CapabilityRegistry(now_ms=lambda: 0, heartbeat_ttl_ms=10_000)
    reg.register(
        CapabilityManifest(
            worker_id=worker_id,
            specialties=(SPECIALTY,),
            base_model="qwen2.5:7b",
            adapters=(),
            tools=("web_fetch",),
            hardware="jetson-orin-nano-super",
            max_concurrent=1,
            eval_score=0.5,
            public_key=b"\x01" * 32,
            schema_version=CURRENT_MANIFEST_VERSION,
        )
    )
    return reg


async def _worker_capturing(transport, worker_id, captured: list[str]) -> None:
    async def handle(msg: MeshMessage) -> None:
        payload = json.loads(msg.payload.decode("utf-8"))
        captured.append(payload["subtask_id"])
        result = TaskResult(
            subtask_id=payload["subtask_id"],
            worker_id=worker_id,
            status="COMPLETED",
            output="ok",
            tokens_used=1,
            latency_ms=1,
            model="qwen2.5:7b",
        )
        await transport.publish(
            MeshMessage(
                request_id=f"r-{payload['subtask_id']}",
                sender_id=worker_id,
                subject=f"subtasks.{payload['subtask_id']}.result",
                payload=json.dumps(result.to_dict()).encode("utf-8"),
                timestamp_ms=1_700_000_000_500,
            )
        )

    await transport.subscribe(f"subtasks.workers.{worker_id}", handle)


@pytest.mark.asyncio
async def test_approved_proposal_runs_next_night_declined_never_does(transports) -> None:
    _, coord, worker = transports
    dispatched: list[str] = []
    await _worker_capturing(worker, "jetson-1", dispatched)

    # A worker's prior run proposed two follow-ups; they land in the holding queue.
    proposed = ProposedQueue()
    result = TaskResult(
        subtask_id="q-origin",
        worker_id="jetson-1",
        status="COMPLETED",
        output="...",
        tokens_used=1,
        latency_ms=1,
        model="m",
        fragment=proposals_to_fragment(
            [
                ("Follow-up A: NF4 internals?", SPECIALTY),
                ("Follow-up B: paged optimisers?", SPECIALTY),
            ]
        ),
    )
    created = proposed.ingest_from_result(
        result, origin_task_id="night-0", origin_question_id="q-origin", now_ms=10
    )
    assert len(created) == 2
    keep, drop = created[0], created[1]

    # Morning review: approve one, decline the other.
    queue = QuestionQueue()
    review = FrontierReview(proposed=proposed, queue=queue)
    promoted = review.approve(keep.proposal_id, now_ms=20)
    review.decline(drop.proposal_id)

    # Next night.
    episodes = EpisodeStore()
    client = SubtaskDispatchClient(
        transport=coord, sender_id="coordinator", now_ms=_now_ms_factory()
    )
    dispatcher = NightlyDispatcher(
        queue=queue,
        dispatch_client=client,
        episode_store=episodes,
        registry=_registry("jetson-1"),
        now_ms=_now_ms_factory(),
        deadline_ms=10_000,
    )

    report = await dispatcher.run_nightly(batch_id="night-1")

    # Only the approved proposal ran; the declined one was never dispatched.
    assert dispatched == [promoted.question_id]
    assert report.dispatched == (promoted.question_id,)
    assert episodes.get(promoted.question_id).task_id == "night-1"
    assert proposed.get(drop.proposal_id).status is ProposalStatus.REJECTED
