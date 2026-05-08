"""DiscordSurfaceIndex — Discord ↔ episode mapping for reaction attribution.

ADR 0006 §5 / issue #115 (slice G1). Two narrow tables in memory now;
persistence lands with the Alembic migration in slice I.

  * `task_messages(message_id PK, task_id, posted_at_ms)`
  * `subtask_threads(thread_id PK, subtask_id, posted_at_ms)`

The reaction listener (G2) and reconnect backfill (G3) read from these
indexes to map an inbound `on_reaction_add` event back to the episode it
should attribute the reward to.

Idempotency: repeated `record_*` calls for the same key are
**last-write-wins**. A second post overwrites the prior task/subtask
mapping, which matches the behavior of a Discord message edit-and-repost.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _TaskMessageRow:
    task_id: str
    posted_at_ms: int


@dataclass(frozen=True)
class _SubtaskThreadRow:
    subtask_id: str
    posted_at_ms: int


class DiscordSurfaceIndex:
    def __init__(self) -> None:
        self._task_messages: dict[str, _TaskMessageRow] = {}
        self._subtask_threads: dict[str, _SubtaskThreadRow] = {}

    def record_task_message(
        self, message_id: str, task_id: str, posted_at_ms: int
    ) -> None:
        self._task_messages[message_id] = _TaskMessageRow(task_id, posted_at_ms)

    def record_subtask_thread(
        self, thread_id: str, subtask_id: str, posted_at_ms: int
    ) -> None:
        self._subtask_threads[thread_id] = _SubtaskThreadRow(subtask_id, posted_at_ms)

    def task_for_message(self, message_id: str) -> str | None:
        row = self._task_messages.get(message_id)
        return row.task_id if row else None

    def subtask_for_thread(self, thread_id: str) -> str | None:
        row = self._subtask_threads.get(thread_id)
        return row.subtask_id if row else None
