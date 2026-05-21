"""First real-broker contract test for ``PresenceService``.

Same assertion as the in-memory presence test (mutual discovery between two
nodes), but the bytes traverse an actual ``nats:2.10`` broker via the
``nats_url`` fixture. Proves the contract end-to-end on the same transport
production runs on.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from turing.mesh.node import MeshNode
from turing.mesh.presence import PresenceService
from turing.transport.nats_bus import NatsBus

pytestmark = pytest.mark.integration


def _make_node(node_id: str, name: str, caps: list[str] | None = None) -> MeshNode:
    node = MeshNode(SimpleNamespace(node_id=node_id, node_name=name))
    node.capabilities = caps or []
    return node


@pytest.mark.asyncio
async def test_two_nodes_discover_each_other_via_real_nats(nats_url: str) -> None:
    bus_a = await NatsBus.connect(url=nats_url, tls_enabled=False, nkey_seed=None, lan_only=False)
    bus_b = await NatsBus.connect(url=nats_url, tls_enabled=False, nkey_seed=None, lan_only=False)
    node_a = _make_node("a", "pi-alpha", ["shell"])
    node_b = _make_node("b", "pi-beta", ["search"])
    pres_a = PresenceService(node_a, bus_a, heartbeat_interval=0.05, stale_after=60.0)
    pres_b = PresenceService(node_b, bus_b, heartbeat_interval=0.05, stale_after=60.0)

    await pres_a.start()
    await pres_b.start()
    try:
        for _ in range(200):
            await asyncio.sleep(0.05)
            if node_a.get_peer("b") and node_b.get_peer("a"):
                break

        peer_b = node_a.get_peer("b")
        peer_a = node_b.get_peer("a")
        assert peer_b is not None, "node_a never saw node_b over real NATS"
        assert peer_a is not None, "node_b never saw node_a over real NATS"
        assert peer_b.name == "pi-beta"
        assert peer_b.capabilities == ["search"]
        assert peer_a.name == "pi-alpha"
        assert peer_a.capabilities == ["shell"]
    finally:
        await pres_a.stop()
        await pres_b.stop()
        await bus_a.close()
        await bus_b.close()
