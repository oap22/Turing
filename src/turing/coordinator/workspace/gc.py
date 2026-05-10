"""WorkspaceGC — evict task workspaces after the retention window.

The coordinator calls :meth:`note_task_completed` when a task closes; ``run``
sweeps and deletes every workspace blob whose owning task completed more than
``retention_hours`` ago. A blob marked via :meth:`mark_for_archive` is handed
to the optional ``archive`` callback before deletion so episode-relevant
training data can be persisted out-of-band.

The GC is pure on the wallclock — ``run(now=...)`` is the only time-aware
entry point so tests can advance the clock deterministically.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from turing.coordinator.workspace.ref import WorkspaceRef

if TYPE_CHECKING:
    from turing.coordinator.workspace.client import WorkspaceClient

ArchiveFn = Callable[[WorkspaceRef, bytes], None]


class WorkspaceGC:
    def __init__(
        self,
        *,
        client: WorkspaceClient,
        retention_hours: int,
        archive: ArchiveFn | None = None,
    ) -> None:
        self._client = client
        self._retention = timedelta(hours=retention_hours)
        self._archive = archive
        self._completed_at: dict[str, datetime] = {}
        self._archive_marks: set[tuple[str, str]] = set()

    def note_task_completed(self, *, task_id: str, at: datetime) -> None:
        self._completed_at[task_id] = at

    def mark_for_archive(self, ref: WorkspaceRef) -> None:
        self._archive_marks.add((ref.task_id, ref.key))

    def run(self, *, now: datetime) -> list[str]:
        """Evict every task whose completion time is past retention.

        Returns the list of task IDs swept. Tasks never marked complete are
        left alone — a long-running task's workspace is never collected just
        because wallclock has elapsed.
        """
        evicted: list[str] = []
        for task_id, completed_at in list(self._completed_at.items()):
            if now - completed_at < self._retention:
                continue
            for key in list(self._client.keys_for_task(task_id)):
                ref = WorkspaceRef(task_id=task_id, key=key)
                if self._archive and (task_id, key) in self._archive_marks:
                    blob = self._client.get(ref)
                    self._archive(ref, blob)
                self._client.delete(ref)
                self._archive_marks.discard((task_id, key))
            evicted.append(task_id)
            del self._completed_at[task_id]
        return evicted
