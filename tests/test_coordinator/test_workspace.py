"""Tests for the workspace addressing layer (#10)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from turing.coordinator.workspace import (
    InMemoryWorkspaceClient,
    InvalidWorkspaceRefError,
    WorkspaceGC,
    WorkspaceRef,
    auto_promote,
)


# ── WorkspaceRef ──────────────────────────────────────────────────────


class TestWorkspaceRef:
    def test_parses_canonical_uri(self) -> None:
        ref = WorkspaceRef.parse("workspace://task-abc/results.json")
        assert ref.task_id == "task-abc"
        assert ref.key == "results.json"

    def test_str_round_trip(self) -> None:
        ref = WorkspaceRef(task_id="task-123", key="payload.bin")
        assert str(ref) == "workspace://task-123/payload.bin"
        assert WorkspaceRef.parse(str(ref)) == ref

    def test_rejects_non_workspace_scheme(self) -> None:
        with pytest.raises(InvalidWorkspaceRefError):
            WorkspaceRef.parse("https://task-abc/key")

    def test_rejects_traversal_in_key(self) -> None:
        with pytest.raises(InvalidWorkspaceRefError):
            WorkspaceRef.parse("workspace://task-abc/../escape")

    def test_rejects_traversal_in_task_id(self) -> None:
        with pytest.raises(InvalidWorkspaceRefError):
            WorkspaceRef(task_id="../etc", key="x")

    def test_is_workspace_uri_quick_check(self) -> None:
        assert WorkspaceRef.is_workspace_uri("workspace://t/k")
        assert not WorkspaceRef.is_workspace_uri("plain text")
        assert not WorkspaceRef.is_workspace_uri("workspace://")


# ── InMemoryWorkspaceClient ───────────────────────────────────────────


class TestInMemoryWorkspaceClient:
    def test_put_then_get_round_trips(self) -> None:
        client = InMemoryWorkspaceClient()
        ref = WorkspaceRef(task_id="t-1", key="k")
        client.put(ref, b"hello")
        assert client.get(ref) == b"hello"

    def test_get_missing_raises(self) -> None:
        client = InMemoryWorkspaceClient()
        with pytest.raises(KeyError):
            client.get(WorkspaceRef(task_id="t-1", key="absent"))

    def test_delete_makes_get_fail(self) -> None:
        client = InMemoryWorkspaceClient()
        ref = WorkspaceRef(task_id="t-1", key="k")
        client.put(ref, b"x")
        client.delete(ref)
        with pytest.raises(KeyError):
            client.get(ref)

    def test_keys_for_task_lists_only_that_task(self) -> None:
        client = InMemoryWorkspaceClient()
        client.put(WorkspaceRef(task_id="t-1", key="a"), b"a")
        client.put(WorkspaceRef(task_id="t-1", key="b"), b"b")
        client.put(WorkspaceRef(task_id="t-2", key="c"), b"c")
        assert sorted(client.keys_for_task("t-1")) == ["a", "b"]


# ── auto_promote ──────────────────────────────────────────────────────


class TestAutoPromote:
    def test_payload_below_threshold_is_passed_through(self) -> None:
        client = InMemoryWorkspaceClient()
        out = auto_promote(
            client=client,
            task_id="t-1",
            key="payload",
            payload=b"hi",
            threshold_bytes=1024,
        )
        assert out == b"hi"  # inline, no promotion

    def test_payload_above_threshold_is_promoted_to_ref(self) -> None:
        client = InMemoryWorkspaceClient()
        big = b"x" * 4096
        out = auto_promote(
            client=client,
            task_id="t-1",
            key="payload",
            payload=big,
            threshold_bytes=1024,
        )
        assert isinstance(out, str)
        assert WorkspaceRef.is_workspace_uri(out)
        ref = WorkspaceRef.parse(out)
        assert client.get(ref) == big


# ── GC ────────────────────────────────────────────────────────────────


def _now(*, days: int = 0, hours: int = 0) -> datetime:
    return datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc) + timedelta(
        days=days, hours=hours
    )


class TestWorkspaceGC:
    def test_gc_drops_entries_for_completed_tasks_past_window(self) -> None:
        client = InMemoryWorkspaceClient()
        ref = WorkspaceRef(task_id="t-old", key="payload")
        client.put(ref, b"data")
        gc = WorkspaceGC(client=client, retention_hours=24)
        gc.note_task_completed(task_id="t-old", at=_now())

        removed = gc.run(now=_now(hours=25))
        assert "t-old" in removed
        with pytest.raises(KeyError):
            client.get(ref)

    def test_gc_keeps_entries_within_window(self) -> None:
        client = InMemoryWorkspaceClient()
        ref = WorkspaceRef(task_id="t-fresh", key="payload")
        client.put(ref, b"data")
        gc = WorkspaceGC(client=client, retention_hours=24)
        gc.note_task_completed(task_id="t-fresh", at=_now())

        removed = gc.run(now=_now(hours=23))
        assert removed == []
        assert client.get(ref) == b"data"

    def test_gc_archives_marked_entries_before_eviction(self) -> None:
        client = InMemoryWorkspaceClient()
        client.put(WorkspaceRef(task_id="t-old", key="train"), b"train data")
        client.put(WorkspaceRef(task_id="t-old", key="scratch"), b"scratch")
        archived: dict[str, bytes] = {}

        def archive(ref: WorkspaceRef, blob: bytes) -> None:
            archived[ref.key] = blob

        gc = WorkspaceGC(client=client, retention_hours=24, archive=archive)
        gc.mark_for_archive(WorkspaceRef(task_id="t-old", key="train"))
        gc.note_task_completed(task_id="t-old", at=_now())
        gc.run(now=_now(hours=25))

        assert archived == {"train": b"train data"}

    def test_gc_does_not_evict_tasks_never_marked_complete(self) -> None:
        client = InMemoryWorkspaceClient()
        ref = WorkspaceRef(task_id="t-running", key="payload")
        client.put(ref, b"data")
        gc = WorkspaceGC(client=client, retention_hours=24)

        # Task is still running — no completion mark
        removed = gc.run(now=_now(hours=200))
        assert removed == []
        assert client.get(ref) == b"data"
