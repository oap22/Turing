"""DAG, planner, and `needs_subtask` contract — see ADR 0001.

Schema version: 1. Bump on breaking changes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
NEEDS_SUBTASK_FRAGMENT_MAX = 5


class Specialty(StrEnum):
    RESEARCH_DISCOVER = "research-discover"
    RESEARCH_DEEP = "research-deep"
    RESEARCH_SUMMARIZE = "research-summarize"
    SYNTHESIS = "synthesis"
    JUDGE = "judge"
    PLANNER = "planner"


class PlannerRoute(StrEnum):
    """Which planning path produced (or should produce) a DAG."""

    TRIVIAL = "trivial"
    LOCAL_13B = "local_13b"
    CLAUDE_SONNET = "claude_sonnet"


class Subtask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^st_[A-Za-z0-9_]+$")
    specialty_required: Specialty
    prompt: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    inputs: dict[str, str] = Field(default_factory=dict)
    output_key: str
    max_retries: int = Field(default=1, ge=0, le=3)
    timeout_s: int = Field(default=120, ge=5, le=1800)

    @field_validator("output_key")
    @classmethod
    def _output_key_is_workspace_uri(cls, v: str) -> str:
        if not v.startswith("workspace://"):
            raise ValueError("output_key must be a workspace:// URI")
        return v

    @field_validator("inputs")
    @classmethod
    def _input_uris_are_workspace(cls, v: dict[str, str]) -> dict[str, str]:
        for key, uri in v.items():
            if not uri.startswith("workspace://"):
                raise ValueError(f"inputs[{key!r}] must be a workspace:// URI")
        return v


class _SubtaskList(BaseModel):
    """Shared graph-validation logic for any list of subtasks."""

    model_config = ConfigDict(extra="forbid")

    subtasks: list[Subtask] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_graph(self) -> Self:
        ids = [s.id for s in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError("subtask ids must be unique")
        id_set = set(ids)
        output_to_id = {s.output_key: s.id for s in self.subtasks}

        # depends_on references must resolve to known ids
        for s in self.subtasks:
            for dep in s.depends_on:
                if dep not in id_set:
                    raise ValueError(f"subtask {s.id!r} depends_on unknown subtask {dep!r}")

        # cycle detection (DFS): white=unvisited, gray=on-stack, black=done
        graph = {s.id: list(s.depends_on) for s in self.subtasks}
        white, gray, black = 0, 1, 2
        colour = {sid: white for sid in graph}

        def visit(node: str) -> None:
            colour[node] = gray
            for nxt in graph[node]:
                if colour[nxt] == gray:
                    raise ValueError(f"cycle detected involving {node!r} -> {nxt!r}")
                if colour[nxt] == white:
                    visit(nxt)
            colour[node] = black

        for sid in graph:
            if colour[sid] == white:
                visit(sid)

        # inputs must reference output_keys of declared dependencies
        for s in self.subtasks:
            allowed_uris = {
                next(out for out, sid in output_to_id.items() if sid == dep) for dep in s.depends_on
            }
            for key, uri in s.inputs.items():
                if uri not in output_to_id:
                    raise ValueError(
                        f"subtask {s.id!r} inputs[{key!r}] references unknown output_key {uri!r}"
                    )
                if uri not in allowed_uris:
                    raise ValueError(
                        f"subtask {s.id!r} inputs[{key!r}] references {uri!r} "
                        f"but does not declare its producer in depends_on"
                    )
        return self

    def topological_order(self) -> list[str]:
        graph = {s.id: list(s.depends_on) for s in self.subtasks}
        order: list[str] = []
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visited:
                return
            for dep in graph[node]:
                visit(dep)
            visited.add(node)
            order.append(node)

        for sid in graph:
            visit(sid)
        return order


class DAG(_SubtaskList):
    task_id: str = Field(pattern=r"^tsk_[A-Za-z0-9_]+$")
    version: Literal[1] = 1


class NeedsSubtaskFragment(_SubtaskList):
    rejoin_after: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_fragment_bounds(self) -> Self:
        if len(self.subtasks) > NEEDS_SUBTASK_FRAGMENT_MAX:
            raise ValueError(f"fragment exceeds max size of {NEEDS_SUBTASK_FRAGMENT_MAX} subtasks")
        ids = {s.id for s in self.subtasks}
        for sid in self.rejoin_after:
            if sid not in ids:
                raise ValueError(f"rejoin_after id {sid!r} not present in fragment subtasks")
        return self


class NeedsSubtaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["needs_subtask"]
    reason: str = Field(min_length=1)
    fragment: NeedsSubtaskFragment
