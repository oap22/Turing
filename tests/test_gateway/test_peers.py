"""Tests for ``GET /peers``.

Slice 1/3 (#215) introduced the ``specs`` block. Slice 2/3 (#216) grew
it to the full PRD schema (static hardware identity + live signals).
The endpoint must report every field on the self-row and forward peer
specs verbatim, while continuing to emit ``specs: null`` for peers
whose heartbeat carried no specs block (rolling upgrade).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth
from turing.mesh.node import MeshNode, PeerInfo
from turing.specs.collector import NodeSpecs


def _specs(
    *,
    model_name: str = "Raspberry Pi 5",
    os_name: str = "linux",
    arch: str = "aarch64",
    cpu_cores: int = 4,
    ram_total_bytes: int = 8 * 1024**3,
    disk_total_bytes: int = 128 * 1024**3,
    cpu_percent: float = 12.0,
    mem_used_bytes: int = 2 * 1024**3,
    disk_used_bytes: int = 10 * 1024**3,
    temp_celsius: float | None = 48.0,
    uptime_seconds: int = 3600,
    loadavg_1m: float = 0.5,
    loadavg_5m: float = 0.4,
    loadavg_15m: float = 0.3,
) -> NodeSpecs:
    return NodeSpecs(
        model_name=model_name,
        os=os_name,
        arch=arch,
        cpu_cores=cpu_cores,
        ram_total_bytes=ram_total_bytes,
        disk_total_bytes=disk_total_bytes,
        cpu_percent=cpu_percent,
        mem_used_bytes=mem_used_bytes,
        disk_used_bytes=disk_used_bytes,
        temp_celsius=temp_celsius,
        uptime_seconds=uptime_seconds,
        loadavg_1m=loadavg_1m,
        loadavg_5m=loadavg_5m,
        loadavg_15m=loadavg_15m,
    )


def _make_mesh(self_specs: NodeSpecs | None) -> MeshNode:
    node = MeshNode(SimpleNamespace(node_id="self-id", node_name="pi-alpha"))
    node.capabilities = ["shell"]
    node.self_specs = self_specs
    return node


_FULL_SPEC_KEYS = frozenset(
    {
        "model_name",
        "os",
        "arch",
        "cpu_cores",
        "ram_total_bytes",
        "disk_total_bytes",
        "cpu_percent",
        "mem_used_bytes",
        "disk_used_bytes",
        "temp_celsius",
        "uptime_seconds",
        "loadavg_1m",
        "loadavg_5m",
        "loadavg_15m",
    }
)


class TestPeersIncludesSpecs:
    @pytest.fixture
    def client(self) -> TestClient:
        mesh = _make_mesh(self_specs=_specs())
        mesh.add_peer(
            PeerInfo(
                node_id="peer-pi",
                name="pi-beta",
                capabilities=["search"],
                specs=_specs(model_name="Raspberry Pi 4", cpu_percent=20.5, temp_celsius=55.5),
            )
        )
        # Mac peer: no temp.
        mesh.add_peer(
            PeerInfo(
                node_id="peer-mac",
                name="mbp",
                capabilities=["judge"],
                specs=_specs(
                    model_name="MacBookPro18,3",
                    os_name="darwin",
                    arch="arm64",
                    cpu_cores=10,
                    ram_total_bytes=16 * 1024**3,
                    disk_total_bytes=512 * 1024**3,
                    cpu_percent=8.1,
                    temp_celsius=None,
                ),
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

    def test_self_row_carries_full_specs(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        self_row = next(p for p in body["peers"] if p["self"])
        assert set(self_row["specs"].keys()) == _FULL_SPEC_KEYS
        assert self_row["specs"]["model_name"] == "Raspberry Pi 5"
        assert self_row["specs"]["cpu_cores"] == 4

    def test_peer_with_full_specs(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        row = next(p for p in body["peers"] if p["node_id"] == "peer-pi")
        assert set(row["specs"].keys()) == _FULL_SPEC_KEYS
        assert row["specs"]["cpu_percent"] == 20.5
        assert row["specs"]["temp_celsius"] == 55.5
        # Bytes-precision: not megabytes/gigabytes.
        assert row["specs"]["ram_total_bytes"] == 8 * 1024**3

    def test_mac_peer_has_null_temp(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        row = next(p for p in body["peers"] if p["node_id"] == "peer-mac")
        assert row["specs"]["temp_celsius"] is None
        assert row["specs"]["os"] == "darwin"
        assert row["specs"]["model_name"] == "MacBookPro18,3"

    def test_legacy_peer_specs_is_null(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        row = next(p for p in body["peers"] if p["node_id"] == "peer-old")
        assert row["specs"] is None

    def test_count_field_preserved(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        assert body["count"] == 4

    def test_existing_fields_unchanged(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        for p in body["peers"]:
            assert {"node_id", "node_name", "self", "capabilities", "last_seen"} <= p.keys()

    def test_every_peer_carries_stale_boolean(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        for p in body["peers"]:
            assert "stale" in p
            assert isinstance(p["stale"], bool)

    def test_self_row_is_never_stale(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        self_row = next(p for p in body["peers"] if p["self"])
        assert self_row["stale"] is False

    def test_fresh_peers_marked_not_stale(self, client: TestClient) -> None:
        # The fixture builds peers via PeerInfo() which touches last_seen to
        # the current time, so they should report stale=false.
        body = client.get("/peers").json()
        for p in body["peers"]:
            if p["self"]:
                continue
            if p["node_id"] == "peer-old":
                # Legacy peer is also fresh (recent add_peer); just present
                # without specs. We're separately covering specs=None above.
                continue
            assert p["stale"] is False

    def test_field_types_match_schema(self, client: TestClient) -> None:
        body = client.get("/peers").json()
        row = next(p for p in body["peers"] if p["node_id"] == "peer-pi")
        s = row["specs"]
        assert isinstance(s["model_name"], str)
        assert isinstance(s["os"], str)
        assert isinstance(s["arch"], str)
        assert isinstance(s["cpu_cores"], int)
        assert isinstance(s["ram_total_bytes"], int)
        assert isinstance(s["disk_total_bytes"], int)
        assert isinstance(s["cpu_percent"], float)
        assert isinstance(s["mem_used_bytes"], int)
        assert isinstance(s["disk_used_bytes"], int)
        assert isinstance(s["uptime_seconds"], int)
        assert isinstance(s["loadavg_1m"], float)
        assert isinstance(s["loadavg_5m"], float)
        assert isinstance(s["loadavg_15m"], float)
        assert s["temp_celsius"] is None or isinstance(s["temp_celsius"], float)


class TestPeersWithNoMesh:
    """Gateway boots without a mesh node — self-row should still be valid."""

    def test_self_row_specs_null_without_mesh(self) -> None:
        app = create_app(auth=GatewayAuth(token="t"), node_name="solo")
        client = TestClient(app)
        body = client.get("/peers").json()
        assert body["count"] == 1
        assert body["peers"][0]["self"] is True
        assert body["peers"][0]["specs"] is None
