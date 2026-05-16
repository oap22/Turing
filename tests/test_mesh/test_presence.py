"""Tests for the NATS-backed mesh presence service.

Brings up two in-process ``PresenceService`` instances sharing an
:class:`InMemoryBus` and asserts mutual discovery via the
``mesh.presence.heartbeat`` subject, plus peer eviction on
``mesh.presence.leave``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from turing.mesh.node import MeshNode
from turing.mesh.presence import (
    HEARTBEAT_SUBJECT,
    LEAVE_SUBJECT,
    PresenceService,
)
from turing.transport.bus import InMemoryBus


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
