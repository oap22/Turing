"""WorkspaceClient protocol + in-memory fake.

The production client wraps NATS object store; the in-memory implementation
keeps unit tests free of network deps. Both expose put/get/delete keyed by
:class:`WorkspaceRef` plus a per-task listing so the GC can find every blob
to evict.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from turing.coordinator.workspace.ref import WorkspaceRef


class WorkspaceClient(Protocol):
    def put(self, ref: WorkspaceRef, blob: bytes) -> None: ...
    def get(self, ref: WorkspaceRef) -> bytes: ...
    def delete(self, ref: WorkspaceRef) -> None: ...
    def keys_for_task(self, task_id: str) -> Iterable[str]: ...


class InMemoryWorkspaceClient:
    def __init__(self) -> None:
        self._blobs: dict[tuple[str, str], bytes] = {}

    def put(self, ref: WorkspaceRef, blob: bytes) -> None:
        self._blobs[(ref.task_id, ref.key)] = blob

    def get(self, ref: WorkspaceRef) -> bytes:
        return self._blobs[(ref.task_id, ref.key)]

    def delete(self, ref: WorkspaceRef) -> None:
        self._blobs.pop((ref.task_id, ref.key), None)

    def keys_for_task(self, task_id: str) -> Iterable[str]:
        return [k for (t, k) in self._blobs.keys() if t == task_id]
