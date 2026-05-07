"""Tests for Scheduler — picks a worker for a subtask given the live registry."""

from __future__ import annotations

from turing.coordinator.registry import CapabilityRegistry
from turing.coordinator.registry.manifest import (
    CURRENT_MANIFEST_VERSION,
    CapabilityManifest,
)
from turing.coordinator.scheduler import Scheduler, Subtask


def _manifest(worker_id: str, **overrides: object) -> CapabilityManifest:
    base = dict(
        worker_id=worker_id,
        specialties=("research-summarize",),
        base_model="qwen2.5:14b",
        adapters=(),
        tools=("vault_query", "web_fetch"),
        hardware="mbp-m3",
        max_concurrent=2,
        eval_score=0.5,
        public_key=b"\x01" * 32,
        schema_version=CURRENT_MANIFEST_VERSION,
    )
    base.update(overrides)
    return CapabilityManifest(**base)  # type: ignore[arg-type]


def _subtask(
    *, specialty: str = "research-summarize", required_tools: tuple[str, ...] = ()
) -> Subtask:
    return Subtask(
        subtask_id="sub-1",
        specialty_required=specialty,
        required_tools=required_tools,
    )


def _registry() -> CapabilityRegistry:
    return CapabilityRegistry(now_ms=lambda: 0, heartbeat_ttl_ms=10_000)


def test_pick_returns_only_matching_specialty_worker() -> None:
    registry = _registry()
    registry.register(_manifest("worker-1", specialties=("research-summarize",)))
    registry.register(_manifest("worker-2", specialties=("code-review",)))
    scheduler = Scheduler()

    pick = scheduler.pick(_subtask(specialty="code-review"), registry)

    assert pick is not None and pick.worker_id == "worker-2"


def test_pick_breaks_ties_by_highest_eval_score() -> None:
    registry = _registry()
    registry.register(_manifest("worker-low", eval_score=0.3))
    registry.register(_manifest("worker-high", eval_score=0.9))
    registry.register(_manifest("worker-mid", eval_score=0.6))
    scheduler = Scheduler()

    pick = scheduler.pick(_subtask(), registry)

    assert pick is not None and pick.worker_id == "worker-high"


def test_pick_skips_workers_at_max_concurrent() -> None:
    registry = _registry()
    registry.register(_manifest("worker-1", max_concurrent=1, eval_score=0.9))
    registry.register(_manifest("worker-2", max_concurrent=1, eval_score=0.5))
    registry.note_dispatch("worker-1")
    scheduler = Scheduler()

    pick = scheduler.pick(_subtask(), registry)

    assert pick is not None and pick.worker_id == "worker-2"


def test_pick_returns_none_when_no_specialty_matches() -> None:
    registry = _registry()
    registry.register(_manifest("worker-1", specialties=("code-review",)))
    scheduler = Scheduler()

    assert scheduler.pick(_subtask(specialty="research-summarize"), registry) is None


def test_pick_returns_none_when_required_tool_unavailable() -> None:
    registry = _registry()
    registry.register(_manifest("worker-1", tools=("vault_query",)))
    scheduler = Scheduler()

    assert (
        scheduler.pick(_subtask(required_tools=("shell",)), registry) is None
    )
