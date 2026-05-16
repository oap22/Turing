"""Tests for ``turing.mesh.discovery.PeerDiscovery`` using a fake Pyre."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from turing.mesh import discovery as discovery_module
from turing.mesh.discovery import PeerDiscovery
from turing.mesh.node import MeshNode

from .conftest import FakePyre


def _make_node() -> MeshNode:
    return MeshNode(SimpleNamespace(node_id="n1", node_name="pi-alpha"))


@pytest.fixture
def fake_pyre(monkeypatch):
    """Patch the ``Pyre`` import inside discovery with a captured FakePyre instance."""
    created: list[FakePyre] = []

    def factory(name: str) -> FakePyre:
        fp = FakePyre(name=name)
        created.append(fp)
        return fp

    monkeypatch.setattr(discovery_module, "Pyre", factory, raising=False)
    monkeypatch.setattr(discovery_module, "PYRE_AVAILABLE", True)
    return created


@pytest.mark.asyncio
class TestPeerDiscoveryStartStop:
    async def test_start_without_pyre_runs_in_degraded_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(discovery_module, "PYRE_AVAILABLE", False)
        node = _make_node()
        disc = PeerDiscovery(node, config=SimpleNamespace())
        await disc.start()
        assert disc.is_running is True
        await disc.stop()
        assert disc.is_running is False

    async def test_start_sets_headers_joins_group_and_starts_pyre(self, fake_pyre) -> None:
        node = _make_node()
        node.capabilities = ["shell"]
        disc = PeerDiscovery(node, config=SimpleNamespace())
        await disc.start()

        fp = fake_pyre[0]
        assert fp.headers["node_id"] == "n1"
        assert fp.headers["node_name"] == "pi-alpha"
        assert json.loads(fp.headers["capabilities"]) == ["shell"]
        assert PeerDiscovery.DISCOVERY_GROUP in fp.joins
        assert fp.started is True
        assert disc.is_running is True

        await disc.stop()
        assert fp.stopped is True
        assert PeerDiscovery.DISCOVERY_GROUP in fp.leaves

    async def test_start_when_already_running_is_noop(self, fake_pyre) -> None:
        disc = PeerDiscovery(_make_node(), config=SimpleNamespace())
        await disc.start()
        await disc.start()  # second call is a warning-only no-op
        assert len(fake_pyre) == 1
        await disc.stop()

    async def test_start_handles_pyre_exception_gracefully(self, monkeypatch) -> None:
        def factory(name: str) -> FakePyre:
            fp = FakePyre(name=name, raise_on_start=RuntimeError("boom"))
            return fp

        monkeypatch.setattr(discovery_module, "Pyre", factory, raising=False)
        monkeypatch.setattr(discovery_module, "PYRE_AVAILABLE", True)
        disc = PeerDiscovery(_make_node(), config=SimpleNamespace())
        await disc.start()
        assert disc.is_running is False

    async def test_stop_handles_pyre_stop_exception(self, monkeypatch) -> None:
        def factory(name: str) -> FakePyre:
            return FakePyre(name=name, raise_on_stop=RuntimeError("kaboom"))

        monkeypatch.setattr(discovery_module, "Pyre", factory, raising=False)
        monkeypatch.setattr(discovery_module, "PYRE_AVAILABLE", True)
        disc = PeerDiscovery(_make_node(), config=SimpleNamespace())
        await disc.start()
        await disc.stop()  # should swallow the exception
        assert disc.is_running is False


@pytest.mark.asyncio
class TestPeerDiscoveryEvents:
    async def test_enter_event_adds_peer_with_parsed_headers(self, fake_pyre) -> None:
        node = _make_node()
        disc = PeerDiscovery(node, config=SimpleNamespace())
        await disc.start()
        fp = fake_pyre[0]

        fp.inject_enter(
            peer_uuid="uuid-2",
            peer_name="pi-beta",
            headers={
                "node_id": "node-beta",
                "capabilities": json.dumps(["search", "shell"]),
            },
        )
        # Give the discovery loop a tick to process.
        for _ in range(20):
            await asyncio.sleep(0.02)
            if node.get_peer("node-beta") is not None:
                break

        peer = node.get_peer("node-beta")
        assert peer is not None
        assert peer.name == "pi-beta"
        assert peer.capabilities == ["search", "shell"]

        await disc.stop()

    async def test_enter_event_with_invalid_capabilities_json_defaults_empty(
        self, fake_pyre
    ) -> None:
        node = _make_node()
        disc = PeerDiscovery(node, config=SimpleNamespace())
        await disc.start()
        fp = fake_pyre[0]

        fp.inject_enter(
            peer_uuid="uuid-3",
            peer_name="pi-gamma",
            headers={"node_id": "node-gamma", "capabilities": "not-json"},
        )
        for _ in range(20):
            await asyncio.sleep(0.02)
            if node.get_peer("node-gamma") is not None:
                break

        peer = node.get_peer("node-gamma")
        assert peer is not None
        assert peer.capabilities == []
        await disc.stop()

    async def test_exit_event_removes_peer(self, fake_pyre) -> None:
        node = _make_node()
        disc = PeerDiscovery(node, config=SimpleNamespace())
        await disc.start()
        fp = fake_pyre[0]

        fp.inject_enter("uuid-4", "pi-delta", headers={"node_id": "uuid-4"})
        for _ in range(20):
            await asyncio.sleep(0.02)
            if node.get_peer("uuid-4") is not None:
                break
        assert node.get_peer("uuid-4") is not None

        fp.inject_exit("uuid-4", "pi-delta")
        for _ in range(20):
            await asyncio.sleep(0.02)
            if node.get_peer("uuid-4") is None:
                break
        assert node.get_peer("uuid-4") is None
        await disc.stop()

    async def test_shout_and_whisper_events_processed_without_raising(self, fake_pyre) -> None:
        node = _make_node()
        disc = PeerDiscovery(node, config=SimpleNamespace())
        await disc.start()
        fp = fake_pyre[0]

        fp.inject_shout("uuid-5", "pi-eps", b"{}")
        fp.inject_whisper("uuid-5", "pi-eps", b"{}")
        # Just exercise the code paths; no assertion needed beyond no-raise.
        await asyncio.sleep(0.1)
        await disc.stop()

    async def test_poll_pyre_returns_none_when_no_events(self, fake_pyre) -> None:
        disc = PeerDiscovery(_make_node(), config=SimpleNamespace())
        await disc.start()
        # Directly exercise the sync poll path.
        assert disc._poll_pyre() is None
        await disc.stop()

    async def test_poll_pyre_returns_none_when_pyre_is_none(self) -> None:
        disc = PeerDiscovery(_make_node(), config=SimpleNamespace())
        # Never started — _pyre is None.
        assert disc._poll_pyre() is None

    async def test_poll_pyre_swallows_recv_exceptions(self, fake_pyre) -> None:
        disc = PeerDiscovery(_make_node(), config=SimpleNamespace())
        await disc.start()
        fp = fake_pyre[0]
        # Queue something so poll() returns 1, then make recv blow up.
        fp.incoming_events.append([b"ENTER", b"x", b"y", {}])
        fp.raise_on_recv = RuntimeError("recv broken")
        assert disc._poll_pyre() is None
        fp.raise_on_recv = None
        await disc.stop()
