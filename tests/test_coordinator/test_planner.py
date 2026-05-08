"""Tests for the Planner deep module — slice 7/26 (#9), AC1.

The planner takes a user prompt and a CapabilityRegistry, calls an LLM,
parses the response into a DAG, and rejects DAGs that:
  - aren't valid JSON
  - violate the schema (cycles, dangling depends_on, etc.)
  - reference required_tools that no live worker can satisfy
"""

from __future__ import annotations

import json

import pytest

from turing.coordinator.planner.planner import (
    Planner,
    PlannerError,
)
from turing.coordinator.planner.schema import DAG
from turing.coordinator.registry import CapabilityRegistry
from turing.coordinator.registry.manifest import CapabilityManifest
from turing.llm.base import LLMResponse


class _FakeLLM:
    """Returns canned content; records the last call for assertions."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

    async def route(self, *, messages, system="", max_tokens=4096, temperature=0.3, **_):
        self.calls.append({"messages": messages, "system": system, "temperature": temperature})
        return LLMResponse(content=self._content)


def _manifest(
    *,
    worker_id: str,
    specialties: tuple[str, ...],
    tools: tuple[str, ...] = (),
) -> CapabilityManifest:
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


def _registry_with(*manifests: CapabilityManifest) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for m in manifests:
        reg.register(m)
    return reg


def _two_subtask_dag_json(task_id: str = "tsk_demo") -> str:
    return json.dumps(
        {
            "task_id": task_id,
            "version": 1,
            "subtasks": [
                {
                    "id": "st_a",
                    "specialty_required": "research-discover",
                    "prompt": "Find the top arxiv papers on speculative decoding.",
                    "depends_on": [],
                    "required_tools": ["web_fetch"],
                    "inputs": {},
                    "output_key": f"workspace://{task_id}/st_a/result",
                    "max_retries": 1,
                    "timeout_s": 120,
                },
                {
                    "id": "st_b",
                    "specialty_required": "research-summarize",
                    "prompt": "Summarize {{ inputs.papers }}.",
                    "depends_on": ["st_a"],
                    "required_tools": [],
                    "inputs": {"papers": f"workspace://{task_id}/st_a/result"},
                    "output_key": f"workspace://{task_id}/st_b/result",
                    "max_retries": 1,
                    "timeout_s": 180,
                },
            ],
        }
    )


def _full_registry() -> CapabilityRegistry:
    return _registry_with(
        _manifest(
            worker_id="w1",
            specialties=("research-discover",),
            tools=("web_fetch",),
        ),
        _manifest(
            worker_id="w2",
            specialties=("research-summarize",),
            tools=("vault_query",),
        ),
    )


class TestPlanner:
    @pytest.mark.asyncio
    async def test_returns_validated_dag_for_well_formed_response(self):
        llm = _FakeLLM(_two_subtask_dag_json())
        planner = Planner(llm)
        dag = await planner.plan("research SD", registry=_full_registry())
        assert isinstance(dag, DAG)
        assert dag.task_id == "tsk_demo"
        assert [s.id for s in dag.subtasks] == ["st_a", "st_b"]

    @pytest.mark.asyncio
    async def test_strips_code_fences(self):
        body = _two_subtask_dag_json()
        llm = _FakeLLM(f"```json\n{body}\n```")
        planner = Planner(llm)
        dag = await planner.plan("p", registry=_full_registry())
        assert dag.task_id == "tsk_demo"

    @pytest.mark.asyncio
    async def test_rejects_non_json(self):
        llm = _FakeLLM("not a dag, sorry")
        planner = Planner(llm)
        with pytest.raises(PlannerError, match="parse"):
            await planner.plan("p", registry=_full_registry())

    @pytest.mark.asyncio
    async def test_rejects_dag_with_cycle(self):
        cyclic = json.loads(_two_subtask_dag_json())
        # Create a cycle: st_a now depends on st_b too.
        cyclic["subtasks"][0]["depends_on"] = ["st_b"]
        llm = _FakeLLM(json.dumps(cyclic))
        planner = Planner(llm)
        with pytest.raises(PlannerError, match="schema"):
            await planner.plan("p", registry=_full_registry())

    @pytest.mark.asyncio
    async def test_rejects_dangling_depends_on(self):
        dangling = json.loads(_two_subtask_dag_json())
        dangling["subtasks"][1]["depends_on"] = ["st_does_not_exist"]
        llm = _FakeLLM(json.dumps(dangling))
        planner = Planner(llm)
        with pytest.raises(PlannerError, match="schema"):
            await planner.plan("p", registry=_full_registry())

    @pytest.mark.asyncio
    async def test_rejects_when_required_tool_unsatisfiable(self):
        # Registry has a research-discover worker but without 'web_fetch'.
        reg = _registry_with(
            _manifest(
                worker_id="w1",
                specialties=("research-discover",),
                tools=(),
            ),
            _manifest(
                worker_id="w2",
                specialties=("research-summarize",),
                tools=(),
            ),
        )
        llm = _FakeLLM(_two_subtask_dag_json())
        planner = Planner(llm)
        with pytest.raises(PlannerError, match="no worker"):
            await planner.plan("p", registry=reg)

    @pytest.mark.asyncio
    async def test_rejects_when_specialty_unsatisfiable(self):
        # Registry has research-discover but no research-summarize worker.
        reg = _registry_with(
            _manifest(
                worker_id="w1",
                specialties=("research-discover",),
                tools=("web_fetch",),
            ),
        )
        llm = _FakeLLM(_two_subtask_dag_json())
        planner = Planner(llm)
        with pytest.raises(PlannerError, match="no worker"):
            await planner.plan("p", registry=reg)
