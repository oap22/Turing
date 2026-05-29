"""Tests for the proposed follow-up question queue (issue #263, ADR 0009).

Covers: a worker attaches proposals to its result without self-dispatching →
they land in a `proposed` holding queue separate from the runnable queue →
nothing is ever auto-dispatched.
"""

from __future__ import annotations

import json

import pytest

from turing.coordinator.dispatch import SubtaskDispatchClient, TaskResult
from turing.coordinator.flywheel import (
    NightlyDispatcher,
    ProposalStatus,
    ProposedQuestion,
    ProposedQueue,
    QuestionQueue,
    ResearchQuestion,
    proposals_from_result,
    proposals_to_fragment,
)
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


def _result(worker_id: str = "w1", *, proposals=()) -> TaskResult:
    fragment = proposals_to_fragment(proposals) if proposals else None
    return TaskResult(
        subtask_id="q1",
        worker_id=worker_id,
        status="COMPLETED",
        output="answer",
        tokens_used=1,
        latency_ms=1,
        model="m",
        fragment=fragment,
    )


# ── wire format ──────────────────────────────────────────────────────────────


def test_proposals_round_trip_through_result_fragment() -> None:
    result = _result(proposals=[("What is QLoRA?", "ai-ml-generalist"), ("What is DPO?", "")])
    extracted = proposals_from_result(result)
    assert extracted == [
        {"prompt": "What is QLoRA?", "specialty": "ai-ml-generalist"},
        {"prompt": "What is DPO?", "specialty": ""},
    ]


def test_result_without_proposals_yields_empty() -> None:
    assert proposals_from_result(_result()) == []


def test_malformed_proposals_are_skipped() -> None:
    result = TaskResult(
        subtask_id="q1",
        worker_id="w1",
        status="COMPLETED",
        output="o",
        tokens_used=0,
        latency_ms=0,
        model="m",
        fragment={"proposed_questions": [{"prompt": "  "}, "not-a-dict", {"specialty": "x"}]},
    )
    assert proposals_from_result(result) == []


# ── ingestion + provenance ───────────────────────────────────────────────────


def test_ingest_lands_proposals_with_provenance() -> None:
    queue = ProposedQueue()
    result = _result("worker-3", proposals=[("Follow-up?", "")])

    created = queue.ingest_from_result(
        result,
        origin_task_id="night-1",
        origin_question_id="q1",
        now_ms=42,
        default_specialty=SPECIALTY,
    )

    assert len(created) == 1
    prop = queue.pending()[0]
    assert prop.prompt == "Follow-up?"
    assert prop.specialty == SPECIALTY  # inherited the originating specialty
    assert prop.origin_task_id == "night-1"
    assert prop.origin_question_id == "q1"
    assert prop.proposed_by == "worker-3"
    assert prop.status is ProposalStatus.PENDING


def test_status_transitions_are_recorded() -> None:
    queue = ProposedQueue()
    queue.add(
        ProposedQuestion(
            proposal_id="p1",
            prompt="q",
            specialty=SPECIALTY,
            origin_task_id="t",
            origin_question_id="q0",
            proposed_by="w1",
        )
    )
    queue.set_status("p1", ProposalStatus.APPROVED)
    assert queue.get("p1").status is ProposalStatus.APPROVED
    assert queue.pending() == []  # no longer pending


# ── the load-bearing guarantee: no auto-dispatch ─────────────────────────────


def _now_ms_factory(start: int = 1_700_000_000_000):
    counter = {"t": start}

    def now() -> int:
        counter["t"] += 1
        return counter["t"]

    return now


@pytest.mark.asyncio
async def test_proposed_questions_are_never_auto_dispatched() -> None:
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

    dispatched_subjects: list[str] = []

    async def handle(msg: MeshMessage) -> None:
        payload = json.loads(msg.payload.decode("utf-8"))
        dispatched_subjects.append(payload["subtask_id"])
        # Worker answers AND proposes two follow-ups attached to its result.
        result = TaskResult(
            subtask_id=payload["subtask_id"],
            worker_id="w1",
            status="COMPLETED",
            output="answer",
            tokens_used=1,
            latency_ms=1,
            model="m",
            fragment=proposals_to_fragment(
                [("deeper follow-up", SPECIALTY), ("broader follow-up", SPECIALTY)]
            ),
        )
        reply = MeshMessage(
            request_id=f"r-{payload['subtask_id']}",
            sender_id="w1",
            subject=f"subtasks.{payload['subtask_id']}.result",
            payload=json.dumps(result.to_dict()).encode("utf-8"),
            timestamp_ms=1_700_000_000_500,
        )
        await worker.publish(reply)

    await worker.subscribe("subtasks.workers.w1", handle)

    runnable = QuestionQueue()
    runnable.add(ResearchQuestion("q1", "seed question", SPECIALTY, created_at_ms=1))
    runnable.approve("q1")

    proposed = ProposedQueue()
    registry = CapabilityRegistry(now_ms=lambda: 0, heartbeat_ttl_ms=10_000)
    registry.register(
        CapabilityManifest(
            worker_id="w1",
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
    client = SubtaskDispatchClient(transport=coord, sender_id="coordinator", now_ms=now)
    dispatcher = NightlyDispatcher(
        queue=runnable,
        dispatch_client=client,
        episode_store=EpisodeStore(),
        registry=registry,
        now_ms=now,
        deadline_ms=10_000,
        proposed_queue=proposed,
    )

    await dispatcher.run_nightly()

    # Exactly one dispatch happened — the seed question. The two proposals
    # landed in the holding queue and were NOT dispatched.
    assert dispatched_subjects == ["q1"]
    pending = proposed.pending()
    assert len(pending) == 2
    assert all(p.origin_question_id == "q1" for p in pending)
    # And they are not in the runnable queue at all.
    assert [q.question_id for q in runnable.all()] == ["q1"]
