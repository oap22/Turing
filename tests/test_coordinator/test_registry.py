"""Tests for CapabilityRegistry — register / heartbeat / expire / find_workers."""

from __future__ import annotations

import pytest

from turing.coordinator.registry import CapabilityRegistry
from turing.coordinator.registry.manifest import (
    CURRENT_MANIFEST_VERSION,
    CapabilityManifest,
)


def _manifest(worker_id: str = "worker-1", **overrides: object) -> CapabilityManifest:
    base = dict(
        worker_id=worker_id,
        specialties=("research-summarize",),
        base_model="qwen2.5:14b",
        adapters=(),
        tools=("vault_query", "web_fetch"),
        hardware="mbp-m3",
        max_concurrent=2,
        eval_score=0.71,
        public_key=b"\x01" * 32,
        schema_version=CURRENT_MANIFEST_VERSION,
    )
    base.update(overrides)
    return CapabilityManifest(**base)  # type: ignore[arg-type]


@pytest.fixture()
def clock() -> dict[str, int]:
    return {"t": 0}


@pytest.fixture()
def registry(clock: dict[str, int]) -> CapabilityRegistry:
    return CapabilityRegistry(now_ms=lambda: clock["t"], heartbeat_ttl_ms=10_000)


def test_register_then_find_returns_the_worker(registry: CapabilityRegistry) -> None:
    registry.register(_manifest())

    found = registry.find_workers(specialty="research-summarize")

    assert [m.worker_id for m in found] == ["worker-1"]


def test_find_workers_filters_by_specialty(registry: CapabilityRegistry) -> None:
    registry.register(_manifest("worker-1", specialties=("research-summarize",)))
    registry.register(_manifest("worker-2", specialties=("code-review",)))

    found = registry.find_workers(specialty="code-review")

    assert [m.worker_id for m in found] == ["worker-2"]


def test_find_workers_requires_required_tools_subset(registry: CapabilityRegistry) -> None:
    registry.register(_manifest("worker-1", tools=("vault_query",)))
    registry.register(_manifest("worker-2", tools=("vault_query", "web_fetch")))

    found = registry.find_workers(
        specialty="research-summarize",
        required_tools=("web_fetch",),
    )

    assert [m.worker_id for m in found] == ["worker-2"]


def test_find_workers_excludes_busy_when_requested(registry: CapabilityRegistry) -> None:
    registry.register(_manifest("worker-1", max_concurrent=1))
    registry.register(_manifest("worker-2", max_concurrent=2))

    registry.note_dispatch("worker-1")  # now in_flight=1, at capacity

    found = registry.find_workers(
        specialty="research-summarize", exclude_busy=True
    )

    assert [m.worker_id for m in found] == ["worker-2"]


def test_note_complete_frees_capacity(registry: CapabilityRegistry) -> None:
    registry.register(_manifest("worker-1", max_concurrent=1))
    registry.note_dispatch("worker-1")
    registry.note_complete("worker-1")

    found = registry.find_workers(
        specialty="research-summarize", exclude_busy=True
    )

    assert [m.worker_id for m in found] == ["worker-1"]


def test_workers_expire_without_heartbeat(
    registry: CapabilityRegistry, clock: dict[str, int]
) -> None:
    registry.register(_manifest("worker-1"))

    clock["t"] = 11_000  # past 10s ttl
    found = registry.find_workers(specialty="research-summarize")

    assert found == []


def test_heartbeat_refreshes_liveness(
    registry: CapabilityRegistry, clock: dict[str, int]
) -> None:
    registry.register(_manifest("worker-1"))

    clock["t"] = 8_000
    registry.heartbeat("worker-1")
    clock["t"] = 17_000  # 9s after heartbeat — still live

    found = registry.find_workers(specialty="research-summarize")
    assert [m.worker_id for m in found] == ["worker-1"]


def test_heartbeat_for_unknown_worker_raises(registry: CapabilityRegistry) -> None:
    with pytest.raises(KeyError):
        registry.heartbeat("ghost")


def test_re_register_replaces_manifest(registry: CapabilityRegistry) -> None:
    registry.register(_manifest("worker-1", eval_score=0.5))
    registry.register(_manifest("worker-1", eval_score=0.9))

    found = registry.find_workers(specialty="research-summarize")
    assert len(found) == 1
    assert found[0].eval_score == 0.9
