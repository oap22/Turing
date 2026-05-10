"""End-to-end test for TrainerPullAgent using the in-memory bus."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from turing.learning.trainer.agent import TrainerPullAgent
from turing.learning.trainer.config import TrainerConfig
from turing.learning.trainer.object_store import InMemoryObjectStore
from turing.learning.trainer.publisher import TrainerPublisher
from turing.learning.trainer.runner import StubTrainer
from turing.transport.bus import InMemoryBus
from turing.transport.signer import MessageSigner

if TYPE_CHECKING:
    from pathlib import Path


def _make_agent(
    tmp_path: Path,
) -> tuple[TrainerPullAgent, InMemoryBus, InMemoryObjectStore, MessageSigner]:
    bus = InMemoryBus()
    store = InMemoryObjectStore()
    signer = MessageSigner.generate()
    publisher = TrainerPublisher(signer=signer, object_store=store, facility_name="dgx-1")
    config = TrainerConfig(
        facility_name="dgx-1",
        artifact_dir=tmp_path,
    )
    agent = TrainerPullAgent(
        config=config,
        bus=bus,
        trainer=StubTrainer(),
        publisher=publisher,
    )
    return agent, bus, store, signer


def _job_payload() -> bytes:
    return json.dumps(
        {
            "job_id": "j-77",
            "dataset_url": "file:///dev/null",
            "dataset_sha256": "0" * 64,
            "base_model": "qwen2.5-7b",
            "method": "sft",
            "hyperparameters": {},
        }
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_agent_subscribes_to_facility_queue(tmp_path: Path) -> None:
    agent, bus, _, _ = _make_agent(tmp_path)
    await agent.start()
    assert "training.queue.dgx-1" in bus._handlers


@pytest.mark.asyncio
async def test_agent_runs_job_and_publishes_signed_manifest(tmp_path: Path) -> None:
    agent, bus, store, _signer = _make_agent(tmp_path)
    await agent.start()
    await bus.publish("training.queue.dgx-1", _job_payload())
    # InMemoryBus dispatch is sync within publish; the agent's handler awaits
    # training synchronously through StubTrainer.
    await asyncio.sleep(0)

    # An adapter blob ended up in the store
    assert len(store.keys()) == 1
    sha = next(iter(store.keys()))
    assert len(sha) == 64


@pytest.mark.asyncio
async def test_agent_emits_training_events_for_job(tmp_path: Path) -> None:
    agent, bus, _, _ = _make_agent(tmp_path)

    received: list[bytes] = []

    async def capture(payload: bytes) -> None:
        received.append(payload)

    await bus.subscribe("training.events.j-77", capture)
    await agent.start()
    await bus.publish("training.queue.dgx-1", _job_payload())
    await asyncio.sleep(0)

    assert received, "no training events emitted"
    kinds = [json.loads(p)["kind"] for p in received]
    assert "started" in kinds
    assert "complete" in kinds


@pytest.mark.asyncio
async def test_agent_emits_failure_event_when_trainer_raises(tmp_path: Path) -> None:
    bus = InMemoryBus()
    store = InMemoryObjectStore()
    signer = MessageSigner.generate()
    publisher = TrainerPublisher(signer=signer, object_store=store, facility_name="dgx-1")

    class BoomTrainer(StubTrainer):
        def train(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("CUDA OOM")

    config = TrainerConfig(facility_name="dgx-1", artifact_dir=tmp_path)
    agent = TrainerPullAgent(config=config, bus=bus, trainer=BoomTrainer(), publisher=publisher)

    received: list[bytes] = []

    async def capture(payload: bytes) -> None:
        received.append(payload)

    await bus.subscribe("training.events.j-77", capture)
    await agent.start()
    await bus.publish("training.queue.dgx-1", _job_payload())
    await asyncio.sleep(0)

    kinds = [json.loads(p)["kind"] for p in received]
    assert "failed" in kinds
