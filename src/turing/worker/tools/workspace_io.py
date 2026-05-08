"""WorkspaceIO — worker-side wrapper around `WorkspaceClient.get` that records
which keys the worker consumed during a subtask.

Issue #116 / ADR 0006 §3. Each `read(key)` call appends the key to a
per-subtask buffer; the worker executor's terminal-write path serializes the
buffer into `Episode.consumed_keys` before recording the episode. Synthesis
thumb attribution then credits upstream subtasks whose `output_key` is in
that buffer.

Duplicates are intentionally preserved — re-reads are signal about worker
behavior. Set semantics are imposed at attribution time, not here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turing.coordinator.workspace.ref import WorkspaceRef

if TYPE_CHECKING:
    from turing.coordinator.workspace.client import WorkspaceClient


class WorkspaceIO:
    def __init__(self, *, client: WorkspaceClient, task_id: str) -> None:
        self._client = client
        self._task_id = task_id
        self._buffer: list[str] = []

    def read(self, key: str) -> bytes:
        blob = self._client.get(WorkspaceRef(task_id=self._task_id, key=key))
        self._buffer.append(key)
        return blob

    @property
    def consumed_keys(self) -> tuple[str, ...]:
        return tuple(self._buffer)

    def reset(self) -> None:
        """Clear the buffer between subtasks."""
        self._buffer.clear()
