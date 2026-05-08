"""Planner deep module — slice 7/26 (#9).

Takes a user prompt + the live :class:`CapabilityRegistry`, asks an LLM to
emit a typed DAG, validates the response against the schema (cycles,
dangling depends_on, etc. are caught by the schema's ``model_validator``),
and additionally checks that every ``(specialty_required, required_tools)``
pair in the DAG can be satisfied by some currently-known worker.

The planner does **not** dispatch — that's the orchestrator's job. It
returns a validated :class:`DAG` or raises :class:`PlannerError`.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Protocol

import structlog
from pydantic import ValidationError

from turing.coordinator.planner.schema import DAG
from turing.llm.base import LLMResponse, Message, Role

if TYPE_CHECKING:
    from collections.abc import Sequence

    from turing.coordinator.registry import CapabilityRegistry


logger = structlog.get_logger("turing.coordinator.planner")


class PlannerError(Exception):
    """Raised when the planner cannot produce a dispatchable DAG."""


class _LLMLike(Protocol):
    async def route(
        self,
        *,
        messages: Sequence[Message],
        system: str = ...,
        max_tokens: int = ...,
        temperature: float = ...,
    ) -> LLMResponse: ...


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


_SYSTEM_PROMPT = (
    "You decompose a user request into a typed DAG of subtasks for a "
    "research-cluster of specialist workers. Respond with a single JSON "
    "object matching the DAG schema (task_id, version=1, subtasks[]). "
    "Each subtask declares specialty_required, prompt, depends_on, "
    "required_tools, inputs, output_key. Prefer the smallest correct DAG. "
    "Reply with JSON only."
)


class Planner:
    def __init__(self, llm: _LLMLike) -> None:
        self._llm = llm

    async def plan(
        self,
        user_prompt: str,
        *,
        registry: CapabilityRegistry,
    ) -> DAG:
        response = await self._llm.route(
            messages=[Message(role=Role.USER, content=user_prompt)],
            system=_SYSTEM_PROMPT,
            max_tokens=2048,
            temperature=0.2,
        )

        raw = _strip_fences(response.content)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlannerError(f"could not parse LLM response as JSON: {exc}") from exc

        try:
            dag = DAG.model_validate(payload)
        except ValidationError as exc:
            raise PlannerError(f"DAG schema validation failed: {exc}") from exc

        _check_satisfiable(dag, registry)

        logger.info(
            "planner.dag_emitted",
            task_id=dag.task_id,
            n_subtasks=len(dag.subtasks),
        )
        return dag


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip()


def _check_satisfiable(dag: DAG, registry: CapabilityRegistry) -> None:
    for st in dag.subtasks:
        candidates = registry.find_workers(
            specialty=st.specialty_required.value,
            required_tools=st.required_tools,
        )
        if not candidates:
            raise PlannerError(
                f"no worker can satisfy subtask {st.id!r} "
                f"(specialty={st.specialty_required.value!r}, "
                f"required_tools={list(st.required_tools)!r})"
            )
