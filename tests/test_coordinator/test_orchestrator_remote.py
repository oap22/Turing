"""Issue #95: orchestrator routes through SubtaskDispatchClient when the
registry says the picked worker is remote, retries once via
``Scheduler.pick_for_retry`` on FAILED/TIMED_OUT, and leaves the local path
unchanged otherwise.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from turing.coordinator.dispatch import SubtaskDispatchClient, TaskResult
from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.orchestrator import (
    DAGOrchestrator,
    WorkerOutput,
)
from turing.coordinator.planner.schema import DAG
from turing.coordinator.registry import CapabilityRegistry
from turing.coordinator.registry.manifest import CapabilityManifest
from turing.coordinator.scheduler.scheduler import Scheduler
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner


def _manifest(worker_id, specialties=("research-summarize",), eval_score=1.0):
    return CapabilityManifest(
        worker_id=worker_id,
        specialties=specialties,
        base_model="m",
        adapters=(),
        tools=(),
        hardware="cpu",
        max_concurrent=1,
        eval_score=eval_score,
        public_key=b"\x00" * 32,
    )


def _single_subtask_dag() -> DAG:
    return DAG.model_validate(
        {
            "task_id": "tsk_remote",
            "version": 1,
            "subtasks": [
                {
                    "id": "st_only",
                    "specialty_required": "research-summarize",
                    "prompt": "summarize",
                    "depends_on": [],
                    "required_tools": [],
                    "inputs": {},
                    "output_key": "workspace://tsk_remote/st_only/result",
                    "max_retries": 1,
                    "timeout_s": 60,
                }
            ],
        }
    )


class _StubSynth:
    async def synthesize(self, *, user_prompt, leaf_outputs):
        return WorkerOutput(
            output_text="FINAL: " + " | ".join(leaf_outputs.values()),
            latency_ms=1,
            tokens_used=1,
        )


@pytest.fixture()
def transports():
    bus = InMemoryBus()
    coord_signer = MessageSigner.generate()
    worker_signer = MessageSigner.generate()
    coord = SignedTransport(
        bus=bus,
        signer=coord_signer,
        # The single worker key is bound to every stub worker id used below.
        trusted_keys={
            wid: worker_signer.public_key for wid in ("w_remote", "w_bad", "w_bad2", "w_good")
        },
        now_ms=lambda: 1_700_000_000_000,
    )
    worker = SignedTransport(
        bus=bus,
        signer=worker_signer,
        trusted_keys={"coord": coord_signer.public_key},
        now_ms=lambda: 1_700_000_000_000,
    )
    return coord, worker


async def _stub_worker_replies(
    worker_transport: SignedTransport,
    *,
    subject: str,
    statuses: list[str],
    worker_id: str,
):
    """Reply to dispatches on `subject` with the next status in `statuses`."""
    pending = list(statuses)

    async def handle(msg: MeshMessage) -> None:
        payload = json.loads(msg.payload.decode("utf-8"))
        status = pending.pop(0) if pending else "FAILED"
        result = TaskResult(
            subtask_id=payload["subtask_id"],
            worker_id=worker_id,
            status=status,
            output="ok-from-" + worker_id if status == "COMPLETED" else "",
            tokens_used=1,
            latency_ms=1,
            model="m",
            error=None if status == "COMPLETED" else "boom",
        )
        reply = MeshMessage(
            request_id=f"reply-{payload['subtask_id']}-{worker_id}",
            sender_id=worker_id,
            subject=f"subtasks.{payload['subtask_id']}.result",
            payload=json.dumps(result.to_dict()).encode("utf-8"),
            timestamp_ms=1_700_000_000_010,
        )
        await worker_transport.publish(reply)

    await worker_transport.subscribe(subject, handle)


class TestRegistryIsLocal:
    def test_local_default_true_when_registered_unmarked(self):
        reg = CapabilityRegistry()
        reg.register(_manifest("w1"))
        assert reg.is_local("w1") is True

    def test_remote_when_local_false(self):
        reg = CapabilityRegistry()
        reg.register(_manifest("w1"), local=False)
        assert reg.is_local("w1") is False

    def test_unknown_worker_is_remote(self):
        reg = CapabilityRegistry()
        assert reg.is_local("nope") is False


class TestOrchestratorRemoteDispatch:
    @pytest.mark.asyncio
    async def test_local_path_unchanged_when_picked_worker_is_local(self, transports):
        # Even when scheduler + dispatch_client are wired, a local pick must
        # run via the in-process worker dict and never hit the bus.
        coord, _ = transports
        store = EpisodeStore()

        class _Local:
            calls = 0

            async def execute(self, subtask, inputs):
                _Local.calls += 1
                return WorkerOutput(output_text="local-out", latency_ms=1, tokens_used=1)

        reg = CapabilityRegistry()
        reg.register(_manifest("w_local"))  # local=True default
        client = SubtaskDispatchClient(
            transport=coord, sender_id="coord", now_ms=lambda: 1_700_000_000_000
        )
        orch = DAGOrchestrator(
            episode_store=store,
            worker_for_specialty={"research-summarize": _Local()},
            synthesizer=_StubSynth(),
            dispatch_client=client,
            scheduler=Scheduler(),
        )
        reply = await orch.run(user_prompt="p", dag=_single_subtask_dag(), registry=reg)
        assert reply.startswith("FINAL:")
        assert _Local.calls == 1

    @pytest.mark.asyncio
    async def test_remote_pick_dispatches_over_bus(self, transports):
        coord, worker = transports
        await _stub_worker_replies(
            worker,
            subject="subtasks.workers.w_remote",
            statuses=["COMPLETED"],
            worker_id="w_remote",
        )

        store = EpisodeStore()
        reg = CapabilityRegistry()
        reg.register(_manifest("w_remote"), local=False)

        client = SubtaskDispatchClient(
            transport=coord, sender_id="coord", now_ms=lambda: 1_700_000_000_000
        )
        orch = DAGOrchestrator(
            episode_store=store,
            worker_for_specialty={},  # no local fallback
            synthesizer=_StubSynth(),
            dispatch_client=client,
            scheduler=Scheduler(),
        )

        reply = await asyncio.wait_for(
            orch.run(user_prompt="p", dag=_single_subtask_dag(), registry=reg),
            timeout=5,
        )
        assert "ok-from-w_remote" in reply

        eps = store.query(specialty="research-summarize")
        assert len(eps) == 1
        assert eps[0].outcome is SubtaskState.COMPLETED
        assert eps[0].worker_id == "w_remote"

    @pytest.mark.asyncio
    async def test_remote_failed_triggers_pick_for_retry_and_recovers(self, transports):
        # First worker FAILS, second worker COMPLETES via pick_for_retry.
        coord, worker = transports

        await _stub_worker_replies(
            worker,
            subject="subtasks.workers.w_bad",
            statuses=["FAILED"],
            worker_id="w_bad",
        )
        await _stub_worker_replies(
            worker,
            subject="subtasks.workers.w_good",
            statuses=["COMPLETED"],
            worker_id="w_good",
        )

        store = EpisodeStore()
        reg = CapabilityRegistry()
        # Higher eval_score wins on first pick; lower one is the fallback.
        reg.register(_manifest("w_bad", eval_score=0.9), local=False)
        reg.register(_manifest("w_good", eval_score=0.5), local=False)

        client = SubtaskDispatchClient(
            transport=coord, sender_id="coord", now_ms=lambda: 1_700_000_000_000
        )
        orch = DAGOrchestrator(
            episode_store=store,
            worker_for_specialty={},
            synthesizer=_StubSynth(),
            dispatch_client=client,
            scheduler=Scheduler(),
        )
        reply = await asyncio.wait_for(
            orch.run(user_prompt="p", dag=_single_subtask_dag(), registry=reg),
            timeout=5,
        )
        assert "ok-from-w_good" in reply
        eps = store.query(specialty="research-summarize")
        # One COMPLETED episode from w_good; the FAILED reply from w_bad is
        # not recorded as terminal since retry succeeded.
        assert any(ep.outcome is SubtaskState.COMPLETED and ep.worker_id == "w_good" for ep in eps)

    @pytest.mark.asyncio
    async def test_remote_double_failure_marks_terminal(self, transports):
        coord, worker = transports
        await _stub_worker_replies(
            worker,
            subject="subtasks.workers.w_bad",
            statuses=["FAILED"],
            worker_id="w_bad",
        )
        await _stub_worker_replies(
            worker,
            subject="subtasks.workers.w_bad2",
            statuses=["FAILED"],
            worker_id="w_bad2",
        )

        store = EpisodeStore()
        reg = CapabilityRegistry()
        reg.register(_manifest("w_bad", eval_score=0.9), local=False)
        reg.register(_manifest("w_bad2", eval_score=0.5), local=False)

        client = SubtaskDispatchClient(
            transport=coord, sender_id="coord", now_ms=lambda: 1_700_000_000_000
        )
        orch = DAGOrchestrator(
            episode_store=store,
            worker_for_specialty={},
            synthesizer=_StubSynth(),
            dispatch_client=client,
            scheduler=Scheduler(),
        )
        with pytest.raises(RuntimeError, match="exhausted retries"):
            await asyncio.wait_for(
                orch.run(user_prompt="p", dag=_single_subtask_dag(), registry=reg),
                timeout=5,
            )

        eps = store.query(specialty="research-summarize")
        assert len(eps) == 1
        assert eps[0].outcome is SubtaskState.FAILED
        assert eps[0].success is False
