"""Tests for retry-to-different-worker policy.

PRD stories 12, 13: on FAILED or TIMED_OUT, the coordinator retries the
subtask once on a different worker of the same specialty if available, else
marks terminal. Both episodes (the failure and the success) are recorded so
failure trajectories feed negative training signal.
"""

from __future__ import annotations

from turing.coordinator.lifecycle import (
    SubtaskEvent,
    SubtaskState,
    TaskLifecycle,
)
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
        tools=("vault_query",),
        hardware="mbp-m3",
        max_concurrent=2,
        eval_score=0.5,
        public_key=b"\x01" * 32,
        schema_version=CURRENT_MANIFEST_VERSION,
    )
    base.update(overrides)
    return CapabilityManifest(**base)  # type: ignore[arg-type]


def _registry() -> CapabilityRegistry:
    return CapabilityRegistry(now_ms=lambda: 0, heartbeat_ttl_ms=10_000)


def test_pick_for_retry_returns_different_worker() -> None:
    registry = _registry()
    registry.register(_manifest("worker-1", eval_score=0.9))
    registry.register(_manifest("worker-2", eval_score=0.7))
    scheduler = Scheduler()
    subtask = Subtask(subtask_id="sub-1", specialty_required="research-summarize")

    pick = scheduler.pick_for_retry(subtask, registry, exclude_worker_ids=("worker-1",))

    assert pick is not None and pick.worker_id == "worker-2"


def test_pick_for_retry_returns_none_when_no_other_worker_available() -> None:
    registry = _registry()
    registry.register(_manifest("worker-1"))
    scheduler = Scheduler()
    subtask = Subtask(subtask_id="sub-1", specialty_required="research-summarize")

    pick = scheduler.pick_for_retry(subtask, registry, exclude_worker_ids=("worker-1",))

    assert pick is None


def test_pick_for_retry_excludes_multiple_failed_workers() -> None:
    registry = _registry()
    registry.register(_manifest("worker-1"))
    registry.register(_manifest("worker-2"))
    registry.register(_manifest("worker-3", eval_score=0.95))
    scheduler = Scheduler()
    subtask = Subtask(subtask_id="sub-1", specialty_required="research-summarize")

    pick = scheduler.pick_for_retry(subtask, registry, exclude_worker_ids=("worker-1", "worker-2"))

    assert pick is not None and pick.worker_id == "worker-3"


def test_pick_for_retry_still_breaks_ties_on_eval_score() -> None:
    registry = _registry()
    registry.register(_manifest("worker-1"))
    registry.register(_manifest("worker-low", eval_score=0.2))
    registry.register(_manifest("worker-high", eval_score=0.9))
    scheduler = Scheduler()
    subtask = Subtask(subtask_id="sub-1", specialty_required="research-summarize")

    pick = scheduler.pick_for_retry(subtask, registry, exclude_worker_ids=("worker-1",))

    assert pick is not None and pick.worker_id == "worker-high"


def test_lifecycle_records_retry_attempts() -> None:
    """Story 13 / acceptance: retry attempt counter visible in event stream."""
    lifecycle = TaskLifecycle()

    # First attempt: PENDING → DISPATCHED → RUNNING → FAILED
    for state in (
        SubtaskState.PENDING,
        SubtaskState.DISPATCHED,
        SubtaskState.RUNNING,
        SubtaskState.FAILED,
    ):
        lifecycle.apply(SubtaskEvent("sub-1", state, ts_ms=0))

    assert lifecycle.attempts_for("sub-1") == 1

    # Retry kicks off — re-dispatch onto a different worker reopens the
    # subtask. Lifecycle accepts the re-open via `retry()` rather than the
    # spurious-event guard so the counter is monotonically tracked.
    lifecycle.retry("sub-1")
    for state in (
        SubtaskState.DISPATCHED,
        SubtaskState.RUNNING,
        SubtaskState.COMPLETED,
    ):
        lifecycle.apply(SubtaskEvent("sub-1", state, ts_ms=0))

    assert lifecycle.attempts_for("sub-1") == 2
    assert lifecycle.state_of("sub-1") is SubtaskState.COMPLETED


def test_retry_only_allowed_from_retryable_terminal_state() -> None:
    lifecycle = TaskLifecycle()
    for state in (
        SubtaskState.PENDING,
        SubtaskState.DISPATCHED,
        SubtaskState.RUNNING,
        SubtaskState.COMPLETED,
    ):
        lifecycle.apply(SubtaskEvent("sub-1", state, ts_ms=0))

    import pytest

    with pytest.raises(ValueError, match="retry"):
        lifecycle.retry("sub-1")
