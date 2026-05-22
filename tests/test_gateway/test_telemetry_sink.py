"""Tests for the gateway's telemetry sink — drains mesh → ring buffer + fans out to WS."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig
from turing.gateway.telemetry_sink import TelemetrySink
from turing.mesh.protocol import MeshMessage, MessageType

if TYPE_CHECKING:
    from pathlib import Path


def _telemetry_message(
    *,
    sender_name: str = "pi-beta",
    event_name: str = "llm.complete.end",
    seq: int = 1,
    payload: dict[str, Any] | None = None,
) -> MeshMessage:
    return MeshMessage(
        type=MessageType.TELEMETRY,
        sender_id="node-x",
        sender_name=sender_name,
        payload={
            "event_name": event_name,
            "stream": event_name.rsplit(".", 1)[0],
            "seq": seq,
            "timestamp_ms": 1000 + seq,
            "duration_ms": 50.0,
            "error": None,
            "payload": payload or {},
        },
    )


@pytest.fixture
async def buffer(tmp_path: Path):
    cfg = RingBufferConfig(
        path=tmp_path / "t.db",
        retention_seconds=10**9,
        max_bytes=10**9,
    )
    buf = RingBuffer(cfg)
    await buf.open()
    yield buf
    await buf.close()


class TestSinkDrainsToBuffer:
    @pytest.mark.asyncio
    async def test_telemetry_message_lands_in_buffer(self, buffer: RingBuffer) -> None:
        sink = TelemetrySink(buffer=buffer)
        await sink.on_mesh_message(_telemetry_message())
        rows = await buffer.query()
        assert len(rows) == 1
        assert rows[0]["event_type"] == "llm.complete.end"

    @pytest.mark.asyncio
    async def test_non_telemetry_message_ignored(self, buffer: RingBuffer) -> None:
        sink = TelemetrySink(buffer=buffer)
        await sink.on_mesh_message(
            MeshMessage(
                type=MessageType.HEARTBEAT,
                sender_id="x",
                sender_name="y",
                payload={},
            )
        )
        rows = await buffer.query()
        assert rows == []


class TestSinkFansOutToClients:
    @pytest.mark.asyncio
    async def test_each_connected_client_receives_message_trace_frame(
        self, buffer: RingBuffer
    ) -> None:
        sink = TelemetrySink(buffer=buffer)

        client_a_frames: list[dict] = []
        client_b_frames: list[dict] = []

        async def send_a(frame: dict) -> None:
            client_a_frames.append(frame)

        async def send_b(frame: dict) -> None:
            client_b_frames.append(frame)

        sub_a = sink.subscribe(send_a)
        sub_b = sink.subscribe(send_b)

        await sink.on_mesh_message(_telemetry_message())

        for frames in (client_a_frames, client_b_frames):
            kinds = {f["type"] for f in frames}
            assert "message_trace" in kinds

        sub_a()
        sub_b()

    @pytest.mark.asyncio
    async def test_metric_frame_emitted_alongside_trace(self, buffer: RingBuffer) -> None:
        sink = TelemetrySink(buffer=buffer)
        frames: list[dict] = []

        async def send(f: dict) -> None:
            frames.append(f)

        sink.subscribe(send)
        await sink.on_mesh_message(_telemetry_message())

        kinds = {f["type"] for f in frames}
        assert "message_trace" in kinds
        assert "metric" in kinds

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self, buffer: RingBuffer) -> None:
        sink = TelemetrySink(buffer=buffer)
        frames: list[dict] = []

        async def send(f: dict) -> None:
            frames.append(f)

        unsubscribe = sink.subscribe(send)
        unsubscribe()
        await sink.on_mesh_message(_telemetry_message())
        assert frames == []

    @pytest.mark.asyncio
    async def test_failing_client_does_not_break_others(self, buffer: RingBuffer) -> None:
        sink = TelemetrySink(buffer=buffer)
        good_frames: list[dict] = []

        async def bad(f: dict) -> None:
            raise RuntimeError("client gone")

        async def good(f: dict) -> None:
            good_frames.append(f)

        sink.subscribe(bad)
        sink.subscribe(good)
        await sink.on_mesh_message(_telemetry_message())
        assert good_frames  # still received frames


class TestSinkLastSendMs:
    """The alerts subsystem reads ``last_send_ms`` as the SPA-reachability
    signal: None means no SPA has ever connected (treated as unreachable)."""

    @pytest.mark.asyncio
    async def test_last_send_ms_is_none_before_any_push(self, buffer: RingBuffer) -> None:
        sink = TelemetrySink(buffer=buffer)
        assert sink.last_send_ms is None

    @pytest.mark.asyncio
    async def test_successful_push_bumps_last_send_ms(self, buffer: RingBuffer) -> None:
        sink = TelemetrySink(buffer=buffer)

        async def send(f: dict) -> None:
            return None

        sink.subscribe(send)
        before = int(time.time() * 1000)
        await sink.on_mesh_message(_telemetry_message())
        after = int(time.time() * 1000)
        assert sink.last_send_ms is not None
        assert before <= sink.last_send_ms <= after

    @pytest.mark.asyncio
    async def test_failed_push_does_not_bump_last_send_ms(self, buffer: RingBuffer) -> None:
        sink = TelemetrySink(buffer=buffer)

        async def bad(f: dict) -> None:
            raise RuntimeError("client gone")

        sink.subscribe(bad)
        await sink.on_mesh_message(_telemetry_message())
        # Only a failing subscriber — no push succeeded, so it stays None.
        assert sink.last_send_ms is None
