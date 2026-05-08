"""auto_promote — replace large payloads with workspace refs.

The message bus drops tiny payloads inline; anything past the configured
threshold lands in the workspace store and the message carries the
``workspace://...`` URI so downstream subtasks can fetch lazily.
"""

from __future__ import annotations

from typing import Union

from turing.coordinator.workspace.client import WorkspaceClient
from turing.coordinator.workspace.ref import WorkspaceRef


def auto_promote(
    *,
    client: WorkspaceClient,
    task_id: str,
    key: str,
    payload: bytes,
    threshold_bytes: int,
) -> Union[bytes, str]:
    """Return ``payload`` as-is if small; otherwise upload + return its URI."""
    if len(payload) <= threshold_bytes:
        return payload
    ref = WorkspaceRef(task_id=task_id, key=key)
    client.put(ref, payload)
    return str(ref)
