"""Tests for the /api/events REST endpoint backing the trace pane (#45)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth
from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig

if TYPE_CHECKING:
    from pathlib import Path


def _evt(
    *,
    timestamp_ms: int,
    node_name: str = "pi-alpha",
    event_type: str = "tool.dispatch.end",
    duration_ms: float = 12.0,
    seq: int = 1,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "node_name": node_name,
        "event_type": event_type,
        "seq": seq,
        "timestamp_ms": timestamp_ms,
        "duration_ms": duration_ms,
        "payload": payload or {},
    }


@pytest.fixture
async def populated_buffer(tmp_path: Path):
    cfg = RingBufferConfig(
        path=tmp_path / "t.db",
        retention_seconds=24 * 3600,
        max_bytes=10**9,
    )
    buf = RingBuffer(cfg)
    await buf.open()
    # Seed: 5 events across 2 nodes, 2 event types, varying durations.
    await buf.append(
        _evt(
            timestamp_ms=1000,
            node_name="pi-alpha",
            event_type="tool.dispatch.end",
            duration_ms=10,
            seq=1,
        )
    )
    await buf.append(
        _evt(
            timestamp_ms=2000,
            node_name="pi-alpha",
            event_type="llm.complete.end",
            duration_ms=200,
            seq=1,
        )
    )
    await buf.append(
        _evt(
            timestamp_ms=3000,
            node_name="pi-beta",
            event_type="tool.dispatch.end",
            duration_ms=50,
            seq=1,
        )
    )
    await buf.append(
        _evt(
            timestamp_ms=4000,
            node_name="pi-beta",
            event_type="llm.complete.end",
            duration_ms=900,
            seq=2,
        )
    )
    await buf.append(
        _evt(
            timestamp_ms=5000,
            node_name="pi-alpha",
            event_type="memory.retrieve.end",
            duration_ms=30,
            seq=1,
        )
    )
    yield buf
    await buf.close()


@pytest.fixture
def client(populated_buffer: RingBuffer) -> TestClient:
    app = create_app(
        auth=GatewayAuth(token="secret"),
        node_name="pi-alpha",
        ring_buffer=populated_buffer,
    )
    return TestClient(app)


# ── auth ─────────────────────────────────────────────────────────────


class TestAuth:
    def test_no_token_returns_401(self, client: TestClient) -> None:
        r = client.get("/api/events")
        assert r.status_code == 401

    def test_correct_token_returns_200(self, client: TestClient) -> None:
        r = client.get("/api/events", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200


# ── filters ──────────────────────────────────────────────────────────


class TestFilters:
    def test_filter_by_node(self, client: TestClient) -> None:
        r = client.get(
            "/api/events?node=pi-beta",
            headers={"Authorization": "Bearer secret"},
        )
        assert r.status_code == 200
        events = r.json()["events"]
        assert all(e["node_name"] == "pi-beta" for e in events)
        assert len(events) == 2

    def test_filter_by_multiple_event_types(self, client: TestClient) -> None:
        r = client.get(
            "/api/events?event_type=tool.dispatch.end&event_type=memory.retrieve.end",
            headers={"Authorization": "Bearer secret"},
        )
        events = r.json()["events"]
        types = {e["event_type"] for e in events}
        assert types == {"tool.dispatch.end", "memory.retrieve.end"}

    def test_filter_by_min_duration(self, client: TestClient) -> None:
        r = client.get(
            "/api/events?min_duration_ms=100",
            headers={"Authorization": "Bearer secret"},
        )
        events = r.json()["events"]
        assert all(e["duration_ms"] >= 100 for e in events)
        assert len(events) == 2

    def test_filter_since_ms(self, client: TestClient) -> None:
        r = client.get(
            "/api/events?since_ms=3000",
            headers={"Authorization": "Bearer secret"},
        )
        events = r.json()["events"]
        assert all(e["timestamp_ms"] >= 3000 for e in events)


# ── pagination ───────────────────────────────────────────────────────


class TestPagination:
    def test_limit_caps_response(self, client: TestClient) -> None:
        r = client.get(
            "/api/events?limit=2",
            headers={"Authorization": "Bearer secret"},
        )
        body = r.json()
        assert len(body["events"]) == 2
        assert body["next_offset"] == 2

    def test_offset_skips_earlier_rows(self, client: TestClient) -> None:
        first = client.get(
            "/api/events?limit=2",
            headers={"Authorization": "Bearer secret"},
        ).json()
        second = client.get(
            "/api/events?limit=2&offset=2",
            headers={"Authorization": "Bearer secret"},
        ).json()
        first_ids = {e["timestamp_ms"] for e in first["events"]}
        second_ids = {e["timestamp_ms"] for e in second["events"]}
        assert first_ids.isdisjoint(second_ids)

    def test_next_offset_null_when_exhausted(self, client: TestClient) -> None:
        r = client.get(
            "/api/events?offset=4",
            headers={"Authorization": "Bearer secret"},
        )
        body = r.json()
        assert len(body["events"]) <= 2
        # When the response itself returns the tail, next_offset should be None.
        assert body["next_offset"] is None


# ── payload shape ────────────────────────────────────────────────────


class TestPayloadShape:
    def test_event_includes_required_fields(self, client: TestClient) -> None:
        r = client.get(
            "/api/events",
            headers={"Authorization": "Bearer secret"},
        )
        events = r.json()["events"]
        for ev in events:
            for field in ("timestamp_ms", "node_name", "event_type", "duration_ms", "payload"):
                assert field in ev

    def test_payload_is_passed_through_unmodified(
        self, client: TestClient, populated_buffer: RingBuffer
    ) -> None:
        # Append a fresh event with a known payload structure
        async def add() -> None:
            await populated_buffer.append(
                _evt(
                    timestamp_ms=9999,
                    node_name="pi-alpha",
                    event_type="llm.complete.end",
                    payload={
                        "provider": "cloud",
                        "model": "claude",
                        "prompt_sample": "[REDACTED:email]",
                    },
                )
            )

        asyncio.get_event_loop().run_until_complete(add())

        r = client.get(
            "/api/events?since_ms=9999",
            headers={"Authorization": "Bearer secret"},
        )
        events = r.json()["events"]
        assert len(events) == 1
        assert events[0]["payload"]["provider"] == "cloud"
        assert events[0]["payload"]["prompt_sample"] == "[REDACTED:email]"
