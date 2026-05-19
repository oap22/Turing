"""Tests for ``GET /peers``.

Specs-panel slice 1/3 (#215) extends the per-peer payload with a
``specs`` block. The endpoint must report the self-row's live specs,
forward peer specs verbatim, and continue to emit ``specs: null`` for
peers whose heartbeat carried no specs block (rolling upgrade).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth
from turing.mesh.node import MeshNode, PeerInfo
from turing.specs.collector import NodeSpecs


def _make_mesh(self_specs: NodeSpecs | None) -> MeshNode:
    node = MeshNode(SimpleNamespace(node_id="self-id", node_name="pi-alpha"))
    node.capabilities = ["shell"]
    node.self_specs = self_specs
    return node


class TestPeersIncludesSpecs:
    @pytest.fixture
    def client(self) -> TestClient:
        mesh = _make_mesh(self_specs=NodeSpecs(cpu_percent=12.0, temp_celsius=48.0))
        # Pi-style peer with full specs.
        mesh.add_peer(
            PeerInfo(
                node_id="peer-pi",
                name="pi-beta",
                capabilities=["search"],
                specs=NodeSpecs(cpu_percent=20.5, temp_celsius=55.5),
            )
        )
        # Mac-style peer: temp is None.
        mesh.add_peer(
            PeerInfo(
                node_id="peer-mac",
                name="mbp",
                capabilities=["judge"],
                specs=NodeSpecs(cpu_percent=8.1, temp_celsius=None),
            )
        )
        # Legacy peer: no specs at all (rolling upgrade).
        mesh.add_peer(PeerInfo(node_id="peer-old", name="pi-old", capabilities=[], specs=None))
        app = create_app(
            auth=GatewayAuth(token="t"),
            node_name="pi-alpha",
            mesh_node=mesh,
        )
        return TestClient(app)

    def test_self_row_carries_specs(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        self_row = next(p for p in body["peers"] if p["self"])
        assert self_row["specs"] == {"cpu_percent": 12.0, "temp_celsius": 48.0}

    def test_peer_with_full_specs(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        row = next(p for p in body["peers"] if p["node_id"] == "peer-pi")
        assert row["specs"] == {"cpu_percent": 20.5, "temp_celsius": 55.5}

    def test_mac_peer_has_null_temp(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        row = next(p for p in body["peers"] if p["node_id"] == "peer-mac")
        assert row["specs"] == {"cpu_percent": 8.1, "temp_celsius": None}

    def test_legacy_peer_specs_is_null(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        row = next(p for p in body["peers"] if p["node_id"] == "peer-old")
        assert row["specs"] is None

    def test_count_field_preserved(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        # self + three peers
        assert body["count"] == 4

    def test_existing_fields_unchanged(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        for p in body["peers"]:
            assert {"node_id", "node_name", "self", "capabilities", "last_seen"} <= p.keys()


class TestPeersWithNoMesh:
    """Gateway boots without a mesh node — self-row should still be valid."""

    def test_self_row_specs_null_without_mesh(self) -> None:
        app = create_app(auth=GatewayAuth(token="t"), node_name="solo")
        client = TestClient(app)
        body = client.get("/peers").json()
        assert body["count"] == 1
        assert body["peers"][0]["self"] is True
        assert body["peers"][0]["specs"] is None
