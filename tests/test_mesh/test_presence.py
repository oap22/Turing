"""Tests for the NATS-backed mesh presence service.

Brings up two in-process ``PresenceService`` instances sharing an
:class:`InMemoryBus` and asserts mutual discovery via the
``mesh.presence.heartbeat`` subject, plus peer eviction on
``mesh.presence.leave``.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from turing.mesh.node import MeshNode
from turing.mesh.presence import (
    HEARTBEAT_SUBJECT,
    LEAVE_SUBJECT,
    SCHEMA_VERSION,
    PresenceService,
)
from turing.specs.collector import NodeSpecs
from turing.transport.bus import InMemoryBus


def _specs(cpu_percent: float = 0.0, temp_celsius: float | None = None) -> NodeSpecs:
    """Build a NodeSpecs with sane defaults for the fields that aren't under test."""
    return NodeSpecs(
        model_name="test-host",
        os="linux",
        arch="aarch64",
        cpu_cores=4,
        ram_total_bytes=8 * 1024**3,
        disk_total_bytes=128 * 1024**3,
        cpu_percent=cpu_percent,
        mem_used_bytes=0,
        disk_used_bytes=0,
        temp_celsius=temp_celsius,
        uptime_seconds=0,
        loadavg_1m=0.0,
        loadavg_5m=0.0,
        loadavg_15m=0.0,
    )


def _make_node(node_id: str, name: str, caps: list[str] | None = None) -> MeshNode:
    node = MeshNode(SimpleNamespace(node_id=node_id, node_name=name))
    node.capabilities = caps or []
    return node


@pytest.mark.asyncio
class TestPresenceService:
    async def test_subjects_are_correct(self) -> None:
        assert HEARTBEAT_SUBJECT == "mesh.presence.heartbeat"
        assert LEAVE_SUBJECT == "mesh.presence.leave"

    async def test_two_nodes_discover_each_other(self) -> None:
        bus = InMemoryBus()
        node_a = _make_node("a", "pi-alpha", ["shell"])
        node_b = _make_node("b", "pi-beta", ["search"])

        # Tight cadence for the test — production uses 10s.
        pres_a = PresenceService(node_a, bus, heartbeat_interval=0.05, stale_after=60.0)
        pres_b = PresenceService(node_b, bus, heartbeat_interval=0.05, stale_after=60.0)

        await pres_a.start()
        await pres_b.start()

        try:
            # Wait for mutual discovery — should be near-instant on InMemoryBus.
            for _ in range(50):
                await asyncio.sleep(0.02)
                if node_a.get_peer("b") and node_b.get_peer("a"):
                    break

            peer_b = node_a.get_peer("b")
            peer_a = node_b.get_peer("a")
            assert peer_b is not None, "node_a never saw node_b"
            assert peer_a is not None, "node_b never saw node_a"
            assert peer_b.name == "pi-beta"
            assert peer_b.capabilities == ["search"]
            assert peer_a.name == "pi-alpha"
            assert peer_a.capabilities == ["shell"]
        finally:
            await pres_a.stop()
            await pres_b.stop()

    async def test_leave_message_evicts_peer(self) -> None:
        bus = InMemoryBus()
        node_a = _make_node("a", "pi-alpha")
        node_b = _make_node("b", "pi-beta")

        pres_a = PresenceService(node_a, bus, heartbeat_interval=0.05, stale_after=60.0)
        pres_b = PresenceService(node_b, bus, heartbeat_interval=0.05, stale_after=60.0)

        await pres_a.start()
        await pres_b.start()

        for _ in range(50):
            await asyncio.sleep(0.02)
            if node_a.get_peer("b"):
                break
        assert node_a.get_peer("b") is not None

        # Graceful shutdown publishes the leave message.
        await pres_b.stop()

        for _ in range(50):
            await asyncio.sleep(0.02)
            if node_a.get_peer("b") is None:
                break
        assert node_a.get_peer("b") is None
        await pres_a.stop()

    async def test_stale_peers_pruned(self) -> None:
        bus = InMemoryBus()
        node_a = _make_node("a", "pi-alpha")
        node_b = _make_node("b", "pi-beta")

        pres_a = PresenceService(node_a, bus, heartbeat_interval=0.05, stale_after=0.1)
        pres_b = PresenceService(node_b, bus, heartbeat_interval=0.05, stale_after=0.1)

        await pres_a.start()
        await pres_b.start()

        for _ in range(50):
            await asyncio.sleep(0.02)
            if node_a.get_peer("b"):
                break
        assert node_a.get_peer("b") is not None

        # Force-stop B without leave by cancelling heartbeats and backdating
        # last_seen so the prune sweep on A evicts it.
        await pres_b._shutdown_without_leave()  # type: ignore[attr-defined]
        peer = node_a.get_peer("b")
        assert peer is not None
        peer.last_seen = time.time() - 10.0

        for _ in range(50):
            await asyncio.sleep(0.02)
            if node_a.get_peer("b") is None:
                break
        assert node_a.get_peer("b") is None
        await pres_a.stop()

    async def test_specs_round_trip_via_heartbeat(self) -> None:
        # Test alpha (in-memory variant): A's collected specs reach B's PeerInfo.specs.
        bus = InMemoryBus()
        node_a = _make_node("a", "pi-alpha")
        node_b = _make_node("b", "pi-beta")

        pres_a = PresenceService(node_a, bus, heartbeat_interval=0.05, stale_after=60.0)
        pres_b = PresenceService(node_b, bus, heartbeat_interval=0.05, stale_after=60.0)

        pinned = _specs(cpu_percent=33.3, temp_celsius=49.5)
        pres_a._sample_self_specs = lambda: pinned  # type: ignore[method-assign]

        await pres_a.start()
        await pres_b.start()
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                peer = node_b.get_peer("a")
                if peer is not None and peer.specs is not None:
                    break
            peer = node_b.get_peer("a")
            assert peer is not None
            assert peer.specs == pinned
            assert node_a.self_specs == pinned
        finally:
            await pres_a.stop()
            await pres_b.stop()

    async def test_heartbeat_missing_specs_field_tolerated(self) -> None:
        # Test eta — rolling-upgrade case: peer publishes a v1 payload with no
        # ``specs`` key; receiver registers it with PeerInfo.specs = None.
        bus = InMemoryBus()
        node_a = _make_node("a", "pi-alpha")
        pres_a = PresenceService(node_a, bus, heartbeat_interval=10.0, stale_after=60.0)

        await pres_a.start()
        try:
            legacy_payload = json.dumps(
                {
                    "schema_version": 1,
                    "node_id": "legacy",
                    "node_name": "pi-legacy",
                    "capabilities": ["search"],
                    "ts_ms": 0,
                }
            ).encode("utf-8")
            await pres_a._on_heartbeat(legacy_payload)
            peer = node_a.get_peer("legacy")
            assert peer is not None
            assert peer.name == "pi-legacy"
            assert peer.specs is None
        finally:
            await pres_a.stop()

    async def test_published_heartbeat_includes_specs_block(self) -> None:
        bus = InMemoryBus()
        node_a = _make_node("a", "pi-alpha")
        pres_a = PresenceService(node_a, bus, heartbeat_interval=10.0, stale_after=60.0)
        pres_a._sample_self_specs = lambda: _specs(  # type: ignore[method-assign]
            cpu_percent=10.0, temp_celsius=None
        )

        captured: list[bytes] = []

        async def grab(raw: bytes) -> None:
            captured.append(raw)

        await bus.subscribe(HEARTBEAT_SUBJECT, grab)
        await pres_a._publish_heartbeat()
        await asyncio.sleep(0.02)

        assert captured, "no heartbeat captured"
        msg = json.loads(captured[-1].decode("utf-8"))
        assert msg["schema_version"] == SCHEMA_VERSION
        assert msg["specs"]["cpu_percent"] == 10.0
        assert msg["specs"]["temp_celsius"] is None
        # Static fields ride along on every heartbeat too.
        assert msg["specs"]["model_name"] == "test-host"
        assert msg["specs"]["cpu_cores"] == 4
