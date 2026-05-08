"""Tests for the DAG orchestrator — slice 7/26 (#9), AC2/AC3.

End-to-end: a 2-subtask DAG runs concurrently, the leaf outputs feed a
synthesis call, and four episodes (planner + 2 subtasks + synthesis) land
in the EpisodeStore with shared ``task_id`` lineage.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.orchestrator import (
    DAGOrchestrator,
    WorkerOutput,
)
from turing.coordinator.planner.schema import DAG
from turing.coordinator.registry import CapabilityRegistry
from turing.coordinator.registry.manifest import CapabilityManifest


def _manifest(worker_id, specialties, tools=()):
    return CapabilityManifest(
        worker_id=worker_id,
        specialties=specialties,
        base_model="m",
        adapters=(),
        tools=tools,
        hardware="cpu",
        max_concurrent=1,
        eval_score=1.0,
        public_key=b"\x00" * 32,
    )


def _registry():
    reg = CapabilityRegistry()
    reg.register(_manifest("w_disc", ("research-discover",), ("web_fetch",)))
    reg.register(_manifest("w_sum", ("research-summarize",), ()))
    reg.register(_manifest("w_syn", ("synthesis",), ()))
    return reg


def _two_subtask_dag() -> DAG:
    return DAG.model_validate(
        json.loads(
            """
            {
              "task_id": "tsk_e2e",
              "version": 1,
              "subtasks": [
                {
                  "id": "st_a",
                  "specialty_required": "research-discover",
                  "prompt": "find papers",
                  "depends_on": [],
                  "required_tools": ["web_fetch"],
                  "inputs": {},
                  "output_key": "workspace://tsk_e2e/st_a/result",
                  "max_retries": 1,
                  "timeout_s": 60
                },
                {
                  "id": "st_b",
                  "specialty_required": "research-summarize",
                  "prompt": "summarize {{ inputs.papers }}",
                  "depends_on": ["st_a"],
                  "required_tools": [],
                  "inputs": {"papers": "workspace://tsk_e2e/st_a/result"},
                  "output_key": "workspace://tsk_e2e/st_b/result",
                  "max_retries": 1,
                  "timeout_s": 60
                }
              ]
            }
            """
        )
    )


class _RecordingWorker:
    """Returns canned output and records call order."""

    def __init__(self, label: str, log: list[str], delay: float = 0.0):
        self.label = label
        self.log = log
        self.delay = delay

    async def execute(self, subtask, inputs):
        self.log.append(f"start:{subtask.id}")
        if self.delay:
            await asyncio.sleep(self.delay)
        self.log.append(f"finish:{subtask.id}")
        text = f"{self.label}-output:" + ",".join(f"{k}={v}" for k, v in inputs.items())
        return WorkerOutput(output_text=text, latency_ms=10, tokens_used=5)


class _StubSynth:
    async def synthesize(self, *, user_prompt, leaf_outputs):
        return WorkerOutput(
            output_text="FINAL: " + " | ".join(leaf_outputs.values()),
            latency_ms=20,
            tokens_used=8,
        )


class TestDAGOrchestrator:
    @pytest.mark.asyncio
    async def test_runs_dag_and_records_four_episodes(self):
        store = EpisodeStore()
        log: list[str] = []
        worker_a = _RecordingWorker("A", log)
        worker_b = _RecordingWorker("B", log)
        orch = DAGOrchestrator(
            episode_store=store,
            worker_for_specialty={
                "research-discover": worker_a,
                "research-summarize": worker_b,
            },
            synthesizer=_StubSynth(),
        )
        dag = _two_subtask_dag()

        reply = await orch.run(
            user_prompt="research SD",
            dag=dag,
            registry=_registry(),
        )

        assert reply.startswith("FINAL:")

        # All four episodes written, all share task_id, all terminal.
        eps_disc = store.query(specialty="research-discover")
        eps_sum = store.query(specialty="research-summarize")
        eps_syn = store.query(specialty="synthesis")
        eps_pln = store.query(specialty="planner")
        assert len(eps_disc) == 1
        assert len(eps_sum) == 1
        assert len(eps_syn) == 1
        assert len(eps_pln) == 1

        all_eps = eps_disc + eps_sum + eps_syn + eps_pln
        assert {ep.task_id for ep in all_eps} == {"tsk_e2e"}
        assert all(ep.outcome is SubtaskState.COMPLETED for ep in all_eps)
        # Synthesis subtask_id is well-known and distinct.
        assert eps_syn[0].subtask_id == "st_synthesis"
        assert eps_pln[0].subtask_id == "st_planner"

    @pytest.mark.asyncio
    async def test_independent_subtasks_run_concurrently(self):
        # A DAG with two independent subtasks should overlap, not serialize.
        store = EpisodeStore()
        log: list[str] = []
        # Both worker-A and worker-B have a 50ms delay; running serially they
        # would take ~100ms. Concurrently, ~50ms.
        worker_a = _RecordingWorker("A", log, delay=0.05)
        worker_b = _RecordingWorker("B", log, delay=0.05)
        orch = DAGOrchestrator(
            episode_store=store,
            worker_for_specialty={
                "research-discover": worker_a,
                "research-deep": worker_b,
            },
            synthesizer=_StubSynth(),
        )
        dag = DAG.model_validate(
            {
                "task_id": "tsk_par",
                "version": 1,
                "subtasks": [
                    {
                        "id": "st_a",
                        "specialty_required": "research-discover",
                        "prompt": "p",
                        "depends_on": [],
                        "required_tools": [],
                        "inputs": {},
                        "output_key": "workspace://tsk_par/st_a/result",
                    },
                    {
                        "id": "st_b",
                        "specialty_required": "research-deep",
                        "prompt": "p",
                        "depends_on": [],
                        "required_tools": [],
                        "inputs": {},
                        "output_key": "workspace://tsk_par/st_b/result",
                    },
                ],
            }
        )
        # No registry validation for this concurrency test — pre-validated DAG.
        await orch.run(user_prompt="p", dag=dag, registry=None)

        # Concurrent execution: both subtasks "start" before either "finish".
        starts = [i for i, e in enumerate(log) if e.startswith("start:")]
        finishes = [i for i, e in enumerate(log) if e.startswith("finish:")]
        assert max(starts) < min(finishes), f"expected overlap, got {log!r}"

    @pytest.mark.asyncio
    async def test_failed_subtask_yields_failed_episode(self):
        store = EpisodeStore()

        class _BoomWorker:
            async def execute(self, subtask, inputs):
                raise RuntimeError("boom")

        orch = DAGOrchestrator(
            episode_store=store,
            worker_for_specialty={
                "research-discover": _BoomWorker(),
                "research-summarize": _RecordingWorker("B", []),
            },
            synthesizer=_StubSynth(),
        )
        dag = _two_subtask_dag()

        with pytest.raises(RuntimeError):
            await orch.run(user_prompt="p", dag=dag, registry=None)

        eps = store.query(specialty="research-discover")
        assert len(eps) == 1
        assert eps[0].outcome is SubtaskState.FAILED
        assert eps[0].success is False
