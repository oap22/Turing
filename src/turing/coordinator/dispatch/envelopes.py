"""SubtaskDispatch + TaskResult envelope dataclasses (ADR 0002, version 1).

Both wrap a JSON payload that the SubtaskDispatchClient publishes inside a
signed MeshMessage. Field set is locked at v1; additive fields are
forward-compatible without bumping `version`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ENVELOPE_VERSION = 1


class SubtaskKind(StrEnum):
    """Routing kinds for `SubtaskDispatch`. Wire format is the string value;
    workers accept arbitrary kind strings (forward-compat) and reject
    unknowns gracefully via `KindRouter`.
    """

    DEFAULT = "default"
    RESEARCH = "research"  # grounded research (ADR 0009 §2 "Night", #261)
    CRITIC_SCORE = "critic_score"
    CANARY_EVAL = "canary_eval"
    EXTRACT_LESSONS = "extract_lessons"


@dataclass(frozen=True)
class SourceInput:
    id: str
    text: str
    url: str | None = None
    title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "url": self.url, "text": self.text, "title": self.title}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SourceInput:
        return cls(id=d["id"], text=d["text"], url=d.get("url"), title=d.get("title"))


@dataclass(frozen=True)
class SubtaskDispatch:
    subtask_id: str
    task_id: str
    specialty: str
    prompt: str
    source_inputs: list[SourceInput]
    deadline_ms: int
    capability_token: dict[str, Any] | None = None
    kind: str = SubtaskKind.DEFAULT.value
    version: int = ENVELOPE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "subtask_id": self.subtask_id,
            "task_id": self.task_id,
            "specialty": self.specialty,
            "prompt": self.prompt,
            "source_inputs": [s.to_dict() for s in self.source_inputs],
            "deadline_ms": self.deadline_ms,
            "capability_token": self.capability_token,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SubtaskDispatch:
        return cls(
            subtask_id=d["subtask_id"],
            task_id=d["task_id"],
            specialty=d["specialty"],
            prompt=d["prompt"],
            source_inputs=[SourceInput.from_dict(s) for s in d.get("source_inputs", [])],
            deadline_ms=d["deadline_ms"],
            capability_token=d.get("capability_token"),
            kind=d.get("kind", SubtaskKind.DEFAULT.value),
            version=d.get("version", ENVELOPE_VERSION),
        )


_VALID_STATUS = frozenset({"COMPLETED", "FAILED", "TIMED_OUT", "REJECTED", "NEEDS_SUBTASK"})


@dataclass(frozen=True)
class TaskResult:
    subtask_id: str
    worker_id: str
    status: str
    output: str
    tokens_used: int
    latency_ms: int
    model: str
    error: str | None = None
    fragment: dict[str, Any] | None = None
    version: int = ENVELOPE_VERSION

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUS:
            raise ValueError(f"status={self.status!r} not in {sorted(_VALID_STATUS)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "subtask_id": self.subtask_id,
            "worker_id": self.worker_id,
            "status": self.status,
            "output": self.output,
            "tokens_used": self.tokens_used,
            "latency_ms": self.latency_ms,
            "model": self.model,
            "error": self.error,
            "fragment": self.fragment,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskResult:
        return cls(
            subtask_id=d["subtask_id"],
            worker_id=d["worker_id"],
            status=d["status"],
            output=d.get("output", ""),
            tokens_used=d.get("tokens_used", 0),
            latency_ms=d.get("latency_ms", 0),
            model=d.get("model", ""),
            error=d.get("error"),
            fragment=d.get("fragment"),
            version=d.get("version", ENVELOPE_VERSION),
        )
