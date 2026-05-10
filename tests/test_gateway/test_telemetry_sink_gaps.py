"""Tests that the gateway TelemetrySink emits gap markers (#46)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig
from turing.gateway.telemetry_sink import TelemetrySink
from turing.mesh.protocol import MeshMessage, MessageType

if TYPE_CHECKING:
    from pathlib import Path


def _msg(seq: int, *, sender: str = "pi-beta") -> MeshMessage:
    return MeshMessage(
        type=MessageType.TELEMETRY,
        sender_id="x",
        sender_name=sender,
        payload={
            "event_name": "tool.dispatch.end",
            "stream": "tool.dispatch",
            "seq": seq,
            "timestamp_ms": 1000 + seq,
            "duration_ms": 12,
            "error": None,
            "payload": {},
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


class TestGapMarkerFanout:
    @pytest.mark.asyncio
    async def test_skipped_seq_emits_gap_marker_frame(self, buffer: RingBuffer) -> None:
        sink = TelemetrySink(buffer=buffer, reorder_window=0)
        frames: list[dict] = []

        async def send(f: dict) -> None:
            frames.append(f)

        sink.subscribe(send)
        await sink.on_mesh_message(_msg(seq=1))
        await sink.on_mesh_message(_msg(seq=3))

        gap_frames = [f for f in frames if f["type"] == "gap_marker"]
        assert len(gap_frames) == 1
        gap = gap_frames[0]
        assert gap["node_name"] == "pi-beta"
        assert gap["stream"] == "tool.dispatch"
        assert gap["missing"] == [2, 2]

    @pytest.mark.asyncio
    async def test_gap_marker_persisted_to_ring_buffer(self, buffer: RingBuffer) -> None:
        sink = TelemetrySink(buffer=buffer, reorder_window=0)
        await sink.on_mesh_message(_msg(seq=1))
        await sink.on_mesh_message(_msg(seq=4))

        rows = await buffer.query(event_type="gap_marker")
        assert len(rows) == 1
        assert rows[0]["payload"]["missing"] == [2, 3]

    @pytest.mark.asyncio
    async def test_clean_sequence_emits_no_gap(self, buffer: RingBuffer) -> None:
        sink = TelemetrySink(buffer=buffer, reorder_window=0)
        frames: list[dict] = []

        async def send(f: dict) -> None:
            frames.append(f)

        sink.subscribe(send)
        for seq in (1, 2, 3, 4):
            await sink.on_mesh_message(_msg(seq=seq))

        assert not [f for f in frames if f["type"] == "gap_marker"]
