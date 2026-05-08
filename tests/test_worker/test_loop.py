"""Tests for the worker subscriber loop (issue #94, ADR 0002 slice 2)."""

from __future__ import annotations

import asyncio
import json

import pytest

from turing.coordinator.dispatch import (
    SourceInput,
    SubtaskDispatch,
    TaskResult,
)
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner
from turing.worker.executor.loop import (
    ExecutorOutput,
    NeedsSubtask,
    WorkerLoop,
)


def _now_factory(start: int = 1_700_000_000_000):
    counter = {"t": start}

    def now() -> int:
        counter["t"] += 1
        return counter["t"]

    return now


@pytest.fixture()
def bus_and_keys():
    bus = InMemoryBus()
    coord_signer = MessageSigner.generate()
    worker_signer = MessageSigner.generate()
    now = _now_factory()
    coord = SignedTransport(
        bus=bus,
        signer=coord_signer,
        trusted_keys=[worker_signer.public_key],
        now_ms=now,
    )
    worker = SignedTransport(
        bus=bus,
        signer=worker_signer,
        trusted_keys=[coord_signer.public_key],
        now_ms=now,
    )
    return bus, coord, worker


def _envelope(
    *,
    subtask_id: str = "st_001",
    specialty: str = "research-summarize",
) -> SubtaskDispatch:
    return SubtaskDispatch(
        subtask_id=subtask_id,
        task_id="t_xyz",
        specialty=specialty,
        prompt="Summarize.",
        source_inputs=[SourceInput(id="src_1", text="text")],
        deadline_ms=1_700_000_010_000,
    )


_publish_seq = {"n": 0}


async def _publish_dispatch(
    coord: SignedTransport,
    envelope: SubtaskDispatch,
    *,
    subject: str,
    timestamp_ms: int = 1_700_000_000_005,
) -> None:
    # Unique request_id per call so the replay window doesn't reject a
    # legitimate JetStream redelivery of the same subtask_id.
    _publish_seq["n"] += 1
    msg = MeshMessage(
        request_id=f"r-{envelope.subtask_id}-{_publish_seq['n']}",
        sender_id="coord",
        subject=subject,
        payload=json.dumps(envelope.to_dict()).encode("utf-8"),
        timestamp_ms=timestamp_ms,
    )
    await coord.publish(msg)


async def _listen_for_result(
    coord: SignedTransport, subtask_id: str
) -> tuple[asyncio.Event, list[TaskResult]]:
    """Subscribe BEFORE publishing the dispatch so the bus has a listener
    when the worker's reply lands."""
    box: list[TaskResult] = []
    event = asyncio.Event()

    async def handle(msg: MeshMessage) -> None:
        box.append(TaskResult.from_dict(json.loads(msg.payload.decode("utf-8"))))
        event.set()

    await coord.subscribe(f"subtasks.{subtask_id}.result", handle)
    return event, box


async def _await_result(event: asyncio.Event, box: list[TaskResult]) -> TaskResult:
    await asyncio.wait_for(event.wait(), timeout=1.0)
    return box[0]


# ─── Status outcomes ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completed_happy_path(bus_and_keys):
    _, coord, worker = bus_and_keys

    async def executor(env: SubtaskDispatch) -> ExecutorOutput:
        return ExecutorOutput.completed(
            output=f"summary for {env.subtask_id}", tokens_used=42, model="m"
        )

    loop = WorkerLoop(
        transport=worker,
        worker_id="w1",
        specialties=("research-summarize",),
        executor=executor,
        now_ms=_now_factory(),
    )
    await loop.start()

    listener = await _listen_for_result(coord, "st_001")
    await _publish_dispatch(coord, _envelope(), subject="subtasks.research-summarize")
    result = await _await_result(*listener)

    assert result.status == "COMPLETED"
    assert result.output == "summary for st_001"
    assert result.worker_id == "w1"
    assert result.tokens_used == 42


@pytest.mark.asyncio
async def test_failed_when_executor_raises(bus_and_keys):
    _, coord, worker = bus_and_keys

    async def executor(_env):
        raise RuntimeError("upstream 503")

    loop = WorkerLoop(
        transport=worker,
        worker_id="w1",
        specialties=("research-summarize",),
        executor=executor,
        now_ms=_now_factory(),
    )
    await loop.start()

    listener = await _listen_for_result(coord, "st_001")
    await _publish_dispatch(coord, _envelope(), subject="subtasks.research-summarize")
    result = await _await_result(*listener)

    assert result.status == "FAILED"
    assert "upstream 503" in (result.error or "")


@pytest.mark.asyncio
async def test_timed_out_when_executor_exceeds_deadline(bus_and_keys):
    _, coord, worker = bus_and_keys

    async def slow_executor(_env):
        await asyncio.sleep(5.0)
        return ExecutorOutput.completed(output="never", tokens_used=0, model="m")

    # Use a now() that thinks current time is well past the deadline, so
    # the loop's wait_for budget is tiny (and clamped to a small floor).
    def now() -> int:
        return 1_700_000_000_000

    loop = WorkerLoop(
        transport=worker,
        worker_id="w1",
        specialties=("research-summarize",),
        executor=slow_executor,
        now_ms=now,
    )
    await loop.start()

    env = _envelope()
    # Deadline already past; loop should clamp to a tiny budget and time out.
    env_at_deadline = SubtaskDispatch(
        subtask_id=env.subtask_id,
        task_id=env.task_id,
        specialty=env.specialty,
        prompt=env.prompt,
        source_inputs=env.source_inputs,
        deadline_ms=1_700_000_000_000,
    )
    listener = await _listen_for_result(coord, "st_001")
    await _publish_dispatch(coord, env_at_deadline, subject="subtasks.research-summarize")
    result = await _await_result(*listener)

    assert result.status == "TIMED_OUT"


@pytest.mark.asyncio
async def test_rejected_when_specialty_not_in_manifest(bus_and_keys):
    _, coord, worker = bus_and_keys

    async def executor(_env):
        raise AssertionError("should not be called for rejected specialty")

    loop = WorkerLoop(
        transport=worker,
        worker_id="w1",
        specialties=("research-summarize",),  # NOT 'judge'
        executor=executor,
        now_ms=_now_factory(),
    )
    await loop.start()

    env = _envelope(specialty="judge")
    # Send via worker-direct subject so the loop is forced to inspect specialty.
    listener = await _listen_for_result(coord, "st_001")
    await _publish_dispatch(coord, env, subject="subtasks.workers.w1")
    result = await _await_result(*listener)

    assert result.status == "REJECTED"
    assert "specialty" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_needs_subtask_response(bus_and_keys):
    _, coord, worker = bus_and_keys

    fragment = {
        "subtasks": [{"id": "st_extra", "specialty_required": "research-deep", "prompt": "..."}]
    }

    async def executor(_env):
        raise NeedsSubtask(reason="need a literature search first", fragment=fragment)

    loop = WorkerLoop(
        transport=worker,
        worker_id="w1",
        specialties=("research-summarize",),
        executor=executor,
        now_ms=_now_factory(),
    )
    await loop.start()

    listener = await _listen_for_result(coord, "st_001")
    await _publish_dispatch(coord, _envelope(), subject="subtasks.research-summarize")
    result = await _await_result(*listener)

    assert result.status == "NEEDS_SUBTASK"
    assert result.fragment == fragment


# ─── Idempotency ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idempotent_on_subtask_id(bus_and_keys):
    _, coord, worker = bus_and_keys
    invocations = {"n": 0}

    async def executor(env):
        invocations["n"] += 1
        return ExecutorOutput.completed(output=f"call-{invocations['n']}", tokens_used=0, model="m")

    loop = WorkerLoop(
        transport=worker,
        worker_id="w1",
        specialties=("research-summarize",),
        executor=executor,
        now_ms=_now_factory(),
    )
    await loop.start()

    # Collect every result that arrives.
    results: list[TaskResult] = []
    event = asyncio.Event()

    async def collect(msg: MeshMessage) -> None:
        results.append(TaskResult.from_dict(json.loads(msg.payload.decode("utf-8"))))
        if len(results) >= 2:
            event.set()

    await coord.subscribe("subtasks.st_001.result", collect)

    # Same envelope delivered twice (different request_ids so replay window
    # accepts both — JetStream redelivery looks like this).
    env = _envelope()
    await _publish_dispatch(
        coord, env, subject="subtasks.research-summarize", timestamp_ms=1_700_000_000_005
    )
    await _publish_dispatch(
        coord, env, subject="subtasks.research-summarize", timestamp_ms=1_700_000_000_006
    )

    await asyncio.wait_for(event.wait(), timeout=1.0)
    assert invocations["n"] == 1, "executor must run once for duplicate subtask_id"
    assert results[0].output == results[1].output == "call-1"
