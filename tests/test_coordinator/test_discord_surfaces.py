"""DiscordSurfaceIndex + surface-write helpers (issue #115, ADR 0006 §5)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from turing.coordinator.discord_surfaces import DiscordSurfaceIndex
from turing.discord_bot.surface_writer import (
    create_subtask_thread,
    post_task_message,
)

# ── DiscordSurfaceIndex ──────────────────────────────────────────────


class TestDiscordSurfaceIndex:
    def test_round_trip_message(self) -> None:
        idx = DiscordSurfaceIndex()
        idx.record_task_message("m1", "t1", 1000)
        assert idx.task_for_message("m1") == "t1"

    def test_round_trip_thread(self) -> None:
        idx = DiscordSurfaceIndex()
        idx.record_subtask_thread("th1", "st1", 2000)
        assert idx.subtask_for_thread("th1") == "st1"

    def test_unknown_lookup_returns_none(self) -> None:
        idx = DiscordSurfaceIndex()
        assert idx.task_for_message("nope") is None
        assert idx.subtask_for_thread("nope") is None

    def test_repeat_record_is_last_write_wins(self) -> None:
        idx = DiscordSurfaceIndex()
        idx.record_task_message("m1", "t-old", 1000)
        idx.record_task_message("m1", "t-new", 2000)
        assert idx.task_for_message("m1") == "t-new"

        idx.record_subtask_thread("th1", "st-old", 1000)
        idx.record_subtask_thread("th1", "st-new", 2000)
        assert idx.subtask_for_thread("th1") == "st-new"


# ── Bot wiring (fake Discord client) ─────────────────────────────────


@dataclass
class _FakeMessage:
    id: str


@dataclass
class _FakeThread:
    id: str


class _FakeChannel:
    def __init__(self, *, message_id: str = "m-42", thread_id: str = "th-7") -> None:
        self._message_id = message_id
        self._thread_id = thread_id
        self.sent: list[str] = []
        self.threads_created: list[str] = []
        self.fail = False

    async def send(self, body: str) -> _FakeMessage:
        if self.fail:
            raise RuntimeError("discord rejected")
        self.sent.append(body)
        return _FakeMessage(id=self._message_id)

    async def create_thread(self, *, name: str) -> _FakeThread:
        if self.fail:
            raise RuntimeError("discord rejected")
        self.threads_created.append(name)
        return _FakeThread(id=self._thread_id)


@pytest.mark.asyncio
async def test_post_task_message_records_row():
    idx = DiscordSurfaceIndex()
    chan = _FakeChannel(message_id="m-100")
    msg = await post_task_message(
        channel=chan,
        body="DAG: pending",
        task_id="t-42",
        index=idx,
        now_ms=lambda: 1234,
    )
    assert msg.id == "m-100"
    assert idx.task_for_message("m-100") == "t-42"


@pytest.mark.asyncio
async def test_post_task_message_does_not_record_on_failure():
    idx = DiscordSurfaceIndex()
    chan = _FakeChannel()
    chan.fail = True
    with pytest.raises(RuntimeError):
        await post_task_message(
            channel=chan,
            body="x",
            task_id="t-1",
            index=idx,
            now_ms=lambda: 1,
        )
    assert idx.task_for_message("m-42") is None


@pytest.mark.asyncio
async def test_create_subtask_thread_records_row():
    idx = DiscordSurfaceIndex()
    chan = _FakeChannel(thread_id="th-99")
    thread = await create_subtask_thread(
        channel=chan,
        name="research summary",
        subtask_id="st-9",
        index=idx,
        now_ms=lambda: 5678,
    )
    assert thread.id == "th-99"
    assert idx.subtask_for_thread("th-99") == "st-9"


@pytest.mark.asyncio
async def test_create_subtask_thread_does_not_record_on_failure():
    idx = DiscordSurfaceIndex()
    chan = _FakeChannel()
    chan.fail = True
    with pytest.raises(RuntimeError):
        await create_subtask_thread(
            channel=chan,
            name="x",
            subtask_id="st-1",
            index=idx,
            now_ms=lambda: 1,
        )
    assert idx.subtask_for_thread("th-7") is None
