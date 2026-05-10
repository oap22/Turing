"""Helpers that post to Discord and record the surface in the index.

Issue #115 wiring. The post-success path writes a row; if Discord rejects
the call, the helper propagates the error and does NOT touch the index.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from turing.coordinator.discord_surfaces import DiscordSurfaceIndex


class _PostsMessages(Protocol):
    async def send(self, body: str) -> Any: ...


class _CreatesThreads(Protocol):
    async def create_thread(self, *, name: str) -> Any: ...


async def post_task_message(
    *,
    channel: _PostsMessages,
    body: str,
    task_id: str,
    index: DiscordSurfaceIndex,
    now_ms: Callable[[], int],
) -> Any:
    """Post the live-DAG message; on success, record the message_id ↔ task_id."""
    message = await channel.send(body)
    index.record_task_message(str(message.id), task_id, now_ms())
    return message


async def create_subtask_thread(
    *,
    channel: _CreatesThreads,
    name: str,
    subtask_id: str,
    index: DiscordSurfaceIndex,
    now_ms: Callable[[], int],
) -> Any:
    """Create a subtask thread; on success, record thread_id ↔ subtask_id."""
    thread = await channel.create_thread(name=name)
    index.record_subtask_thread(str(thread.id), subtask_id, now_ms())
    return thread
