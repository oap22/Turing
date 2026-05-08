"""Tests for SubtaskDispatchClient (issue #93, ADR 0002).

Covers the dispatch + reply correlation flow against an InMemoryBus with a
stub worker subscribing to dispatch subjects and replying with a TaskResult.
Production runs against a real `NatsBus` — same `Bus` Protocol, so behaviour
is exercised the same way.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from turing.coordinator.dispatch import (
    SourceInput,
    SubtaskDispatch,
    SubtaskDispatchClient,
    SubtaskTimeoutError,
    TaskResult,
)
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner


def _now_ms_factory(start: int = 1_700_000_000_000):
    counter = {"t": start}

    def now() -> int:
        counter["t"] += 1
        return counter["t"]

    return now


@pytest.fixture()
def transports():
    """Two SignedTransports sharing one bus, mutually trusting."""
    bus = InMemoryBus()
    coordinator_signer = MessageSigner.generate()
    worker_signer = MessageSigner.generate()
    now = _now_ms_factory()

    coord = SignedTransport(
        bus=bus,
        signer=coordinator_signer,
        trusted_keys=[worker_signer.public_key],
        now_ms=now,
    )
    worker = SignedTransport(
        bus=bus,
        signer=worker_signer,
        trusted_keys=[coordinator_signer.public_key],
        now_ms=now,
    )
    return bus, coord, worker, coordinator_signer, worker_signer


def _envelope(subtask_id: str = "st_001", specialty: str = "research-summarize") -> SubtaskDispatch:
    return SubtaskDispatch(
        subtask_id=subtask_id,
        task_id="t_xyz",
        specialty=specialty,
        prompt="Summarize.",
        source_inputs=[SourceInput(id="src_1", text="some text")],
        deadline_ms=1_700_000_999_999,
    )


async def _stub_worker(
    *,
    transport: SignedTransport,
    subject: str,
    coord_transport: SignedTransport,
    sender_id: str = "stub-worker",
    output: str = "summary text",
    status: str = "COMPLETED",
) -> None:
    """Subscribe to a dispatch subject and reply with a TaskResult."""

    async def handle(msg: MeshMessage) -> None:
        payload = json.loads(msg.payload.decode("utf-8"))
        result = TaskResult(
            subtask_id=payload["subtask_id"],
            worker_id=sender_id,
            status=status,
            output=output,
            tokens_used=10,
            latency_ms=5,
            model="stub",
        )
        result_msg = MeshMessage(
            request_id=f"reply-{payload['subtask_id']}",
            sender_id=sender_id,
            subject=f"subtasks.{payload['subtask_id']}.result",
            payload=json.dumps(result.to_dict()).encode("utf-8"),
            timestamp_ms=1_700_000_000_001,
        )
        await transport.publish(result_msg)

    await transport.subscribe(subject, handle)


# ─── Happy path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_returns_task_result_via_specialty_queue(transports):
    _, coord, worker, *_ = transports
    await _stub_worker(
        transport=worker,
        subject="subtasks.research-summarize",
        coord_transport=coord,
    )

    client = SubtaskDispatchClient(
        transport=coord,
        sender_id="coordinator",
        now_ms=_now_ms_factory(),
    )
    result = await client.dispatch(_envelope(), deadline_ms=1_700_000_010_000)

    assert result.subtask_id == "st_001"
    assert result.status == "COMPLETED"
    assert result.output == "summary text"


@pytest.mark.asyncio
async def test_dispatch_uses_worker_direct_subject_when_worker_id_set(transports):
    _, coord, worker, *_ = transports
    # Worker only subscribes to its direct subject; dispatch must route there.
    await _stub_worker(
        transport=worker,
        subject="subtasks.workers.mbp-premium-1",
        coord_transport=coord,
    )

    client = SubtaskDispatchClient(
        transport=coord,
        sender_id="coordinator",
        now_ms=_now_ms_factory(),
    )
    result = await client.dispatch(
        _envelope(),
        worker_id="mbp-premium-1",
        deadline_ms=1_700_000_010_000,
    )
    assert result.status == "COMPLETED"


# ─── Envelope shapes match ADR 0002 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_envelope_matches_adr_v1_schema(transports):
    bus, coord, _worker, _, _ = transports
    captured: list[bytes] = []

    async def capture(raw: bytes) -> None:
        captured.append(raw)

    # Plain bus subscriber so we see the wire bytes (not the unwrapped payload).
    await bus.subscribe("subtasks.research-summarize", capture)

    client = SubtaskDispatchClient(
        transport=coord, sender_id="coordinator", now_ms=_now_ms_factory()
    )
    # Don't await dispatch (no worker is replying); we only need the publish.
    task = asyncio.create_task(client.dispatch(_envelope(), deadline_ms=1_700_000_000_100))
    await asyncio.sleep(0)  # let publish run
    task.cancel()
    with pytest.raises((asyncio.CancelledError, SubtaskTimeoutError)):
        await task

    assert captured, "publish never happened"
    # The signed-transport frame wraps a MeshMessage; decode payload.
    frame = json.loads(captured[0].decode("utf-8"))
    mesh_payload = bytes.fromhex(frame["p"])
    mesh = MeshMessage.from_bytes(mesh_payload)
    body = json.loads(mesh.payload.decode("utf-8"))

    assert body["version"] == 1
    assert body["subtask_id"] == "st_001"
    assert body["task_id"] == "t_xyz"
    assert body["specialty"] == "research-summarize"
    assert body["prompt"] == "Summarize."
    assert body["source_inputs"] == [
        {"id": "src_1", "url": None, "text": "some text", "title": None}
    ]
    assert body["deadline_ms"] == 1_700_000_999_999
    assert body["capability_token"] is None


def test_task_result_round_trips_adr_v1_schema():
    r = TaskResult(
        subtask_id="st_001",
        worker_id="w1",
        status="COMPLETED",
        output="hi",
        tokens_used=5,
        latency_ms=10,
        model="m",
    )
    d = r.to_dict()
    assert d["version"] == 1
    assert d["status"] == "COMPLETED"
    assert TaskResult.from_dict(d) == r


# ─── Timeout ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_times_out_when_no_result_arrives(transports):
    _, coord, _, *_ = transports
    client = SubtaskDispatchClient(
        transport=coord, sender_id="coordinator", now_ms=lambda: 1_700_000_000_000
    )
    with pytest.raises(SubtaskTimeoutError):
        # deadline already past; client uses 30s grace, but we give 0 grace
        # explicitly so the timeout fires fast.
        await client.dispatch(
            _envelope(),
            deadline_ms=1_700_000_000_000,
            grace_s=0.05,
        )


@pytest.mark.asyncio
async def test_late_result_after_timeout_does_not_crash(transports):
    _bus, coord, worker, _, _ = transports

    # Worker that schedules its reply on a background task so the dispatch
    # publish returns immediately. (InMemoryBus runs handlers inline, so an
    # in-handler `await sleep` would block the publisher and defeat the test.)
    background: list[asyncio.Task] = []

    async def slow_handle(msg: MeshMessage) -> None:
        payload = json.loads(msg.payload.decode("utf-8"))

        async def _delayed_reply() -> None:
            await asyncio.sleep(0.15)  # > grace
            result_msg = MeshMessage(
                request_id="late",
                sender_id="slow",
                subject=f"subtasks.{payload['subtask_id']}.result",
                payload=json.dumps(
                    TaskResult(
                        subtask_id=payload["subtask_id"],
                        worker_id="slow",
                        status="COMPLETED",
                        output="late",
                        tokens_used=0,
                        latency_ms=0,
                        model="m",
                    ).to_dict()
                ).encode("utf-8"),
                timestamp_ms=1_700_000_000_010,
            )
            await worker.publish(result_msg)

        background.append(asyncio.create_task(_delayed_reply()))

    await worker.subscribe("subtasks.research-summarize", slow_handle)

    client = SubtaskDispatchClient(
        transport=coord, sender_id="coordinator", now_ms=lambda: 1_700_000_000_000
    )
    with pytest.raises(SubtaskTimeoutError):
        await client.dispatch(_envelope(), deadline_ms=1_700_000_000_000, grace_s=0.05)

    # Give the late publish time to fire; nothing should crash.
    await asyncio.sleep(0.2)
    for t in background:
        if not t.done():
            t.cancel()


# ─── Concurrency ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_concurrent_dispatches_get_distinct_results(transports):
    _, coord, worker, *_ = transports

    async def echo(msg: MeshMessage) -> None:
        payload = json.loads(msg.payload.decode("utf-8"))
        result = TaskResult(
            subtask_id=payload["subtask_id"],
            worker_id="w",
            status="COMPLETED",
            output=f"out-for-{payload['subtask_id']}",
            tokens_used=0,
            latency_ms=0,
            model="m",
        )
        result_msg = MeshMessage(
            request_id=f"r-{payload['subtask_id']}",
            sender_id="w",
            subject=f"subtasks.{payload['subtask_id']}.result",
            payload=json.dumps(result.to_dict()).encode("utf-8"),
            timestamp_ms=1_700_000_000_010,
        )
        await worker.publish(result_msg)

    await worker.subscribe("subtasks.research-summarize", echo)

    client = SubtaskDispatchClient(
        transport=coord, sender_id="coordinator", now_ms=_now_ms_factory()
    )

    r1, r2 = await asyncio.gather(
        client.dispatch(_envelope("st_a"), deadline_ms=1_700_000_010_000),
        client.dispatch(_envelope("st_b"), deadline_ms=1_700_000_010_000),
    )
    assert r1.output == "out-for-st_a"
    assert r2.output == "out-for-st_b"
