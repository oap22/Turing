"""Tests for the eval harness coordinator_worker (issue #96).

Sync vs async note: the harness's `WorkerFn` is sync, so the public
`coordinator_worker` runs each dispatch inside a fresh event loop. Driving
*two* loops in one test (one for the stub worker subscriber, one inside
the worker) cross-binds asyncio futures and deadlocks. So tests use the
async helper directly on a single event loop; the sync wrapper is
exercised separately with no live worker (timeout path).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from turing.coordinator.dispatch import (
    SubtaskDispatch,
    SubtaskDispatchClient,
    TaskResult,
)
from turing.evals.research_summarize.harness import run
from turing.evals.research_summarize.schema import EvalCase, Expected, SourceDoc
from turing.evals.research_summarize.workers import (
    CoordinatorWorkerConfig,
    coordinator_worker,
    coordinator_worker_async,
)
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner


def _now_factory(start: int = 1_700_000_000_000):
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
    now = _now_factory()
    coord = SignedTransport(
        bus=bus, signer=coord_signer, trusted_keys=[worker_signer.public_key], now_ms=now
    )
    worker = SignedTransport(
        bus=bus, signer=worker_signer, trusted_keys=[coord_signer.public_key], now_ms=now
    )
    return coord, worker


def _case(case_id: str = "test-1") -> EvalCase:
    return EvalCase(
        id=case_id,
        category="claim_preservation",
        prompt="Summarize.",
        source_docs=[SourceDoc(id="src_1", text="some source", title="Note")],
        expected=Expected(must_contain_claims=["some source"]),
        scoring_fn="score_claim_preservation_v1",
    )


async def _stub_worker_subscribe(
    *,
    transport: SignedTransport,
    subject: str,
    output: str = "summary text",
    status: str = "COMPLETED",
) -> None:
    background: list[asyncio.Task] = []

    async def handle(msg: MeshMessage) -> None:
        env = SubtaskDispatch.from_dict(json.loads(msg.payload.decode("utf-8")))

        # Reply via a background task: InMemoryBus runs handlers inline, so
        # publishing the result inside this handler would block the publisher
        # (the SubtaskDispatchClient's own publish call) and dead-lock the
        # await-on-future inside dispatch().
        async def _reply() -> None:
            result = TaskResult(
                subtask_id=env.subtask_id,
                worker_id="stub",
                status=status,
                output=output,
                tokens_used=10,
                latency_ms=5,
                model="stub",
            )
            result_msg = MeshMessage(
                request_id=f"reply-{env.subtask_id}",
                sender_id="stub",
                subject=f"subtasks.{env.subtask_id}.result",
                payload=json.dumps(result.to_dict()).encode("utf-8"),
                timestamp_ms=1_700_000_000_010,
            )
            await transport.publish(result_msg)

        background.append(asyncio.create_task(_reply()))

    await transport.subscribe(subject, handle)


# ─── Round-trip via specialty queue ────────────────────────────────────────


@pytest.mark.asyncio
async def test_coordinator_worker_async_returns_summary_via_specialty_queue(transports):
    coord, worker = transports
    await _stub_worker_subscribe(
        transport=worker,
        subject="subtasks.research-summarize",
        output="round-trip ok",
    )

    client = SubtaskDispatchClient(transport=coord, sender_id="harness", now_ms=_now_factory())
    fn = coordinator_worker_async(
        client=client,
        config=CoordinatorWorkerConfig(specialty="research-summarize"),
    )
    out = await fn(_case())
    assert out == "round-trip ok"


@pytest.mark.asyncio
async def test_coordinator_worker_async_routes_to_worker_id_subject(transports):
    coord, worker = transports
    await _stub_worker_subscribe(
        transport=worker,
        subject="subtasks.workers.mbp-premium-1",
        output="direct ok",
    )

    client = SubtaskDispatchClient(transport=coord, sender_id="harness", now_ms=_now_factory())
    fn = coordinator_worker_async(
        client=client,
        config=CoordinatorWorkerConfig(specialty="research-summarize", worker_id="mbp-premium-1"),
    )
    out = await fn(_case())
    assert out == "direct ok"


def test_coordinator_worker_sync_failure_surfaces_as_score_zero_via_harness(transports):
    """Sync wrapper: when no worker is subscribed and the dispatch times
    out, the harness must score 0 instead of crashing."""
    coord, _worker = transports
    client = SubtaskDispatchClient(
        transport=coord, sender_id="harness", now_ms=lambda: 1_700_000_000_000
    )
    fn = coordinator_worker(
        client=client,
        config=CoordinatorWorkerConfig(
            specialty="research-summarize",
            deadline_offset_s=0.0,  # already past
            grace_s=0.05,
        ),
    )
    report = run(fn, [_case()])
    assert report["per_case"][0]["score"] == 0.0
