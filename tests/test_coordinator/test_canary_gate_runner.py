"""CanaryGateRunner + canary_eval handler over NATS (issue #118)."""

from __future__ import annotations

import hashlib
import json

import pytest

from turing.coordinator.adapters.manifest import AdapterManifest
from turing.coordinator.adapters.registry import AdapterRegistry, AdapterState
from turing.coordinator.dispatch import (
    SubtaskDispatch,
    SubtaskDispatchClient,
    SubtaskKind,
    TaskResult,
)
from turing.coordinator.promotion.canary_gate_runner import CanaryGateRunner
from turing.coordinator.promotion.canary_pass_gate import CanaryPassGate
from turing.coordinator.promotion.canary_selector import CanarySelector
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner
from turing.worker.executor.canary_handler import make_canary_handler

SPECIALTY = "research-summarize"
BASE_MODEL = "qwen2.5-7b"


def _registered_adapter(
    registry: AdapterRegistry, signer: MessageSigner, *, name: str, version: str
) -> AdapterManifest:
    blob = b"adapter-bytes"
    digest = hashlib.sha256(blob).hexdigest()
    base = AdapterManifest(
        name=name,
        version=version,
        base_model=BASE_MODEL,
        sha256=digest,
        eval_score=70.0,
        signer_public_key=signer.public_key,
        signature=b"",
    )
    sig = signer.sign(base.signing_bytes()).signature
    manifest = AdapterManifest(
        name=name,
        version=version,
        base_model=BASE_MODEL,
        sha256=digest,
        eval_score=70.0,
        signer_public_key=signer.public_key,
        signature=sig,
    )
    registry.register(manifest, blob)
    return manifest


def _transports(worker_count: int = 2):
    # Worker i's key is bound to id "w-a", "w-b", ... — _attach_worker calls
    # must use the matching worker_id for the binding check to pass.
    bus = InMemoryBus()
    coord_signer = MessageSigner.generate()
    workers = []
    trusted_for_coord: dict[str, bytes] = {}
    for n in range(worker_count):
        ws = MessageSigner.generate()
        wt = SignedTransport(
            bus=bus,
            signer=ws,
            trusted_keys={"coord": coord_signer.public_key},
            now_ms=lambda: 1_000,
        )
        workers.append(wt)
        trusted_for_coord[f"w-{'abcdefgh'[n]}"] = ws.public_key
    coord = SignedTransport(
        bus=bus,
        signer=coord_signer,
        trusted_keys=trusted_for_coord,
        now_ms=lambda: 1_000,
    )
    return coord, workers


def _attach_worker(transport: SignedTransport, *, worker_id: str, payload: dict):
    """Subscribe worker_id's direct subject and reply with TaskResult(payload)."""

    async def handle(msg: MeshMessage) -> None:
        body = json.loads(msg.payload.decode("utf-8"))
        result = TaskResult(
            subtask_id=body["subtask_id"],
            worker_id=worker_id,
            status="COMPLETED",
            output=json.dumps(payload),
            tokens_used=0,
            latency_ms=0,
            model="canary",
        )
        await transport.publish(
            MeshMessage(
                request_id=f"r-{body['subtask_id']}",
                sender_id=worker_id,
                subject=f"subtasks.{body['subtask_id']}.result",
                payload=json.dumps(result.to_dict()).encode("utf-8"),
                timestamp_ms=1_001,
            )
        )

    return transport.subscribe(f"subtasks.workers.{worker_id}", handle)


# ─── Worker handler ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_canary_handler_returns_scored_payload():
    async def load_fn(_manifest, _path):
        return object()

    async def eval_fn(_adapter, _path):
        return 70.5, [{"subtask_id": "c1"}]

    handler = make_canary_handler(load_fn=load_fn, eval_fn=eval_fn)

    class _Env:
        prompt = json.dumps({"adapter_manifest": {}, "eval_set_path": "evals/x/"})

    out = await handler(_Env())  # type: ignore[arg-type]
    body = json.loads(out["output"])
    assert body["status"] == "scored"
    assert body["score"] == pytest.approx(70.5)
    assert body["failed_cases"] == [{"subtask_id": "c1"}]


@pytest.mark.asyncio
async def test_canary_handler_load_failure():
    async def load_fn(_m, _p):
        raise ValueError("bad sha")

    async def eval_fn(_a, _p):  # pragma: no cover - never reached
        return 0.0, []

    handler = make_canary_handler(load_fn=load_fn, eval_fn=eval_fn)

    class _Env:
        prompt = json.dumps({"adapter_manifest": {}, "eval_set_path": "evals/x/"})

    out = await handler(_Env())  # type: ignore[arg-type]
    body = json.loads(out["output"])
    assert body["status"] == "load_failed"
    assert "bad sha" in body["error"]


@pytest.mark.asyncio
async def test_canary_handler_eval_failure():
    async def load_fn(_m, _p):
        return object()

    async def eval_fn(_a, _p):
        raise RuntimeError("oom")

    handler = make_canary_handler(load_fn=load_fn, eval_fn=eval_fn)

    class _Env:
        prompt = json.dumps({"adapter_manifest": {}, "eval_set_path": "evals/x/"})

    out = await handler(_Env())  # type: ignore[arg-type]
    body = json.loads(out["output"])
    assert body["status"] == "eval_failed"
    assert "oom" in body["error"]


# ─── Gate runner integration ─────────────────────────────────────────


def _runner_setup():
    signer = MessageSigner.generate()
    registry = AdapterRegistry(worker_base_model=BASE_MODEL, trusted_issuers=[signer.public_key])
    return signer, registry


@pytest.mark.asyncio
async def test_first_promotion_passes_with_any_score():
    signer, registry = _runner_setup()
    _registered_adapter(registry, signer, name="research", version="v1")

    coord, workers = _transports(worker_count=1)
    await _attach_worker(
        workers[0],
        worker_id="w-a",
        payload={"status": "scored", "score": 60.0, "failed_cases": [], "error": None},
    )

    runner = CanaryGateRunner(
        registry=registry,
        selector=CanarySelector(),
        pass_gate=CanaryPassGate(epsilon_pp=0.5),
        dispatch_client=SubtaskDispatchClient(
            transport=coord, sender_id="coord", now_ms=lambda: 1_000
        ),
        now_ms=lambda: 1_000,
    )
    outcome = await runner.run(
        name="research",
        version="v1",
        specialty=SPECIALTY,
        fleet=["w-a"],
        adapter_manifest={"name": "research", "version": "v1"},
        eval_set_path="evals/research/",
    )
    assert outcome.promoted is True
    assert outcome.score == pytest.approx(60.0)
    assert outcome.delta_pp is None
    assert registry.state_of(name="research", version="v1") is AdapterState.LIVE
    assert runner.last_canary_worker(SPECIALTY) == "w-a"


@pytest.mark.asyncio
async def test_pass_when_canary_within_epsilon_of_prior():
    signer, registry = _runner_setup()
    # Prior LIVE
    _registered_adapter(registry, signer, name="r", version="v1")
    registry.promote(name="r", version="v1", canary_eval_score=70.0)
    # Candidate
    _registered_adapter(registry, signer, name="r", version="v2")

    coord, workers = _transports(1)
    await _attach_worker(
        workers[0],
        worker_id="w-a",
        payload={"status": "scored", "score": 70.5, "failed_cases": [], "error": None},
    )

    promoted_calls: list = []

    async def on_promoted(name, version, specialty):
        promoted_calls.append((name, version, specialty))

    runner = CanaryGateRunner(
        registry=registry,
        selector=CanarySelector(),
        pass_gate=CanaryPassGate(epsilon_pp=0.5),
        dispatch_client=SubtaskDispatchClient(
            transport=coord, sender_id="coord", now_ms=lambda: 1_000
        ),
        now_ms=lambda: 1_000,
        on_promoted=on_promoted,
    )
    outcome = await runner.run(
        name="r",
        version="v2",
        specialty=SPECIALTY,
        fleet=["w-a"],
        adapter_manifest={"name": "r", "version": "v2"},
        eval_set_path="evals/r/",
    )
    assert outcome.promoted is True
    assert promoted_calls == [("r", "v2", SPECIALTY)]
    assert registry.state_of(name="r", version="v2") is AdapterState.LIVE


@pytest.mark.asyncio
async def test_regression_rejects_and_collects_hard_examples():
    signer, registry = _runner_setup()
    _registered_adapter(registry, signer, name="r", version="v1")
    registry.promote(name="r", version="v1", canary_eval_score=70.0)
    _registered_adapter(registry, signer, name="r", version="v2")

    coord, workers = _transports(1)
    failed_cases = [
        {
            "subtask_id": "c1",
            "input_text": "q1",
            "expected_text": "e1",
            "actual_output": "a1",
            "failure_reason": "wrong",
        },
        {
            "subtask_id": "c2",
            "input_text": "q2",
            "expected_text": "e2",
            "actual_output": "a2",
            "failure_reason": "missing citation",
        },
    ]
    await _attach_worker(
        workers[0],
        worker_id="w-a",
        payload={
            "status": "scored",
            "score": 67.0,
            "failed_cases": failed_cases,
            "error": None,
        },
    )

    rejected_calls: list = []

    async def on_rejected(outcome, name, version, specialty):
        rejected_calls.append((outcome, name, version, specialty))

    runner = CanaryGateRunner(
        registry=registry,
        selector=CanarySelector(),
        pass_gate=CanaryPassGate(epsilon_pp=0.5),
        dispatch_client=SubtaskDispatchClient(
            transport=coord, sender_id="coord", now_ms=lambda: 1_000
        ),
        now_ms=lambda: 1_000,
        on_rejected=on_rejected,
    )
    outcome = await runner.run(
        name="r",
        version="v2",
        specialty=SPECIALTY,
        fleet=["w-a"],
        adapter_manifest={"name": "r", "version": "v2"},
        eval_set_path="evals/r/",
    )
    assert outcome.promoted is False
    assert outcome.status == "regression"
    assert outcome.delta_pp == pytest.approx(-3.0)
    assert len(outcome.hard_examples) == 2
    assert outcome.hard_examples[1].failure_reason == "missing citation"
    assert registry.state_of(name="r", version="v2") is AdapterState.REJECTED
    assert len(rejected_calls) == 1


@pytest.mark.asyncio
async def test_load_failed_rejects_adapter():
    signer, registry = _runner_setup()
    _registered_adapter(registry, signer, name="r", version="v1")

    coord, workers = _transports(1)
    await _attach_worker(
        workers[0],
        worker_id="w-a",
        payload={"status": "load_failed", "score": None, "failed_cases": [], "error": "bad sha"},
    )

    runner = CanaryGateRunner(
        registry=registry,
        selector=CanarySelector(),
        pass_gate=CanaryPassGate(),
        dispatch_client=SubtaskDispatchClient(
            transport=coord, sender_id="coord", now_ms=lambda: 1_000
        ),
        now_ms=lambda: 1_000,
    )
    outcome = await runner.run(
        name="r",
        version="v1",
        specialty=SPECIALTY,
        fleet=["w-a"],
        adapter_manifest={"name": "r", "version": "v1"},
        eval_set_path="evals/r/",
    )
    assert outcome.promoted is False
    assert outcome.status == "load_failed"
    assert registry.state_of(name="r", version="v1") is AdapterState.REJECTED


@pytest.mark.asyncio
async def test_round_robin_selection_advances_between_runs():
    signer, registry = _runner_setup()
    _registered_adapter(registry, signer, name="r", version="v1")
    _registered_adapter(registry, signer, name="r", version="v2")

    coord, workers = _transports(2)
    for ws_t, wid in zip(workers, ["w-a", "w-b"], strict=True):
        await _attach_worker(
            ws_t,
            worker_id=wid,
            payload={"status": "scored", "score": 70.0, "failed_cases": [], "error": None},
        )

    runner = CanaryGateRunner(
        registry=registry,
        selector=CanarySelector(),
        pass_gate=CanaryPassGate(),
        dispatch_client=SubtaskDispatchClient(
            transport=coord, sender_id="coord", now_ms=lambda: 1_000
        ),
        now_ms=lambda: 1_000,
    )
    await runner.run(
        name="r",
        version="v1",
        specialty=SPECIALTY,
        fleet=["w-a", "w-b"],
        adapter_manifest={},
        eval_set_path="evals/r/",
    )
    first = runner.last_canary_worker(SPECIALTY)
    await runner.run(
        name="r",
        version="v2",
        specialty=SPECIALTY,
        fleet=["w-a", "w-b"],
        adapter_manifest={},
        eval_set_path="evals/r/",
    )
    second = runner.last_canary_worker(SPECIALTY)
    assert {first, second} == {"w-a", "w-b"}
    assert first != second


def test_envelope_kind_is_canary_eval():
    # Pure plumbing assertion — easier than parsing the wire each time.
    assert SubtaskKind.CANARY_EVAL.value == "canary_eval"


# Suppress "unused" — included so the import gets used in case the assertion above is moved.
_ = SubtaskDispatch
