"""Tests for the signed NATS-backed mesh presence service (issue #348).

Brings up in-process ``PresenceService`` instances sharing an
:class:`InMemoryBus`, each riding its own :class:`SignedTransport`, and
asserts mutual discovery via ``mesh.presence.heartbeat`` plus peer eviction
on ``mesh.presence.leave``. Security tests cover unsigned frames, untrusted
keys, sender-id spoofing, cross-node leave eviction, replay, the peer-table
cap, and the per-sender heartbeat rate limit.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from turing.coordinator.alerts.dispatcher import AlertDispatcher
from turing.coordinator.alerts.engine import AlertEngine
from turing.mesh.node import MAX_PEERS, MeshNode, PeerInfo
from turing.mesh.presence import (
    HEARTBEAT_SUBJECT,
    LEAVE_SUBJECT,
    SCHEMA_VERSION,
    PresenceService,
)
from turing.specs.collector import NodeSpecs
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner


def _specs(
    cpu_percent: float = 0.0,
    temp_celsius: float | None = None,
    disk_used_bytes: int = 0,
) -> NodeSpecs:
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
        disk_used_bytes=disk_used_bytes,
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


def _now_ms() -> int:
    return int(time.time() * 1000)


class Fleet:
    """A shared InMemoryBus plus one signer/transport per trusted node_id."""

    def __init__(self, node_ids: list[str]) -> None:
        self.bus = InMemoryBus()
        self.signers: dict[str, MessageSigner] = {nid: MessageSigner.generate() for nid in node_ids}
        self.trusted: dict[str, bytes] = {
            nid: signer.public_key for nid, signer in self.signers.items()
        }

    def transport(self, node_id: str) -> SignedTransport:
        return SignedTransport(
            bus=self.bus,
            signer=self.signers[node_id],
            trusted_keys=self.trusted,
            now_ms=_now_ms,
        )

    def untrusted_transport(self) -> SignedTransport:
        """A transport signing with a key that is NOT in the trusted map."""
        return SignedTransport(
            bus=self.bus,
            signer=MessageSigner.generate(),
            trusted_keys=self.trusted,
            now_ms=_now_ms,
        )


def _heartbeat_payload(node_id: str, name: str | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "node_id": node_id,
        "node_name": name or f"node-{node_id}",
        "capabilities": [],
        "ts_ms": _now_ms(),
        "specs": None,
    }
    payload.update(extra)
    return payload


def _envelope(sender_id: str, subject: str, payload: dict[str, Any]) -> MeshMessage:
    return MeshMessage(
        request_id=uuid.uuid4().hex,
        sender_id=sender_id,
        subject=subject,
        payload=json.dumps(payload).encode("utf-8"),
        timestamp_ms=_now_ms(),
    )


def _presence(
    node: MeshNode,
    fleet: Fleet,
    **kwargs: Any,
) -> PresenceService:
    kwargs.setdefault("heartbeat_interval", 0.05)
    kwargs.setdefault("stale_after", 60.0)
    # Tests drive heartbeats far faster than the production 10s cadence, so
    # disable the per-sender floor unless a test exercises it explicitly.
    kwargs.setdefault("heartbeat_min_interval", 0.0)
    return PresenceService(node, fleet.transport(node.node_id), **kwargs)


@pytest.mark.asyncio
class TestPresenceService:
    async def test_subjects_are_correct(self) -> None:
        assert HEARTBEAT_SUBJECT == "mesh.presence.heartbeat"
        assert LEAVE_SUBJECT == "mesh.presence.leave"

    async def test_two_nodes_discover_each_other(self) -> None:
        fleet = Fleet(["a", "b"])
        node_a = _make_node("a", "pi-alpha", ["shell"])
        node_b = _make_node("b", "pi-beta", ["search"])

        # Tight cadence for the test — production uses 10s.
        pres_a = _presence(node_a, fleet)
        pres_b = _presence(node_b, fleet)

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
        fleet = Fleet(["a", "b"])
        node_a = _make_node("a", "pi-alpha")
        node_b = _make_node("b", "pi-beta")

        pres_a = _presence(node_a, fleet)
        pres_b = _presence(node_b, fleet)

        await pres_a.start()
        await pres_b.start()

        for _ in range(50):
            await asyncio.sleep(0.02)
            if node_a.get_peer("b"):
                break
        assert node_a.get_peer("b") is not None

        # Graceful shutdown publishes the signed leave message.
        await pres_b.stop()

        for _ in range(50):
            await asyncio.sleep(0.02)
            if node_a.get_peer("b") is None:
                break
        assert node_a.get_peer("b") is None
        await pres_a.stop()

    async def test_stale_peers_pruned(self) -> None:
        fleet = Fleet(["a", "b"])
        node_a = _make_node("a", "pi-alpha")
        node_b = _make_node("b", "pi-beta")

        pres_a = _presence(node_a, fleet, stale_after=0.1)
        pres_b = _presence(node_b, fleet, stale_after=0.1)

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
        fleet = Fleet(["a", "b"])
        node_a = _make_node("a", "pi-alpha")
        node_b = _make_node("b", "pi-beta")

        pres_a = _presence(node_a, fleet)
        pres_b = _presence(node_b, fleet)

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
        # Rolling-upgrade case within the signed era: a trusted peer publishes
        # a payload with no ``specs`` key; receiver registers it with
        # PeerInfo.specs = None.
        fleet = Fleet(["a", "legacy"])
        node_a = _make_node("a", "pi-alpha")
        pres_a = _presence(node_a, fleet, heartbeat_interval=10.0)

        await pres_a.start()
        try:
            legacy_payload = {
                "schema_version": 2,
                "node_id": "legacy",
                "node_name": "pi-legacy",
                "capabilities": ["search"],
                "ts_ms": 0,
            }
            legacy_transport = fleet.transport("legacy")
            await legacy_transport.publish(_envelope("legacy", HEARTBEAT_SUBJECT, legacy_payload))
            peer = node_a.get_peer("legacy")
            assert peer is not None
            assert peer.name == "pi-legacy"
            assert peer.specs is None
        finally:
            await pres_a.stop()

    async def test_published_heartbeat_includes_specs_block(self) -> None:
        fleet = Fleet(["a", "b"])
        node_a = _make_node("a", "pi-alpha")
        pres_a = _presence(node_a, fleet, heartbeat_interval=10.0)
        pres_a._sample_self_specs = lambda: _specs(  # type: ignore[method-assign]
            cpu_percent=10.0, temp_celsius=None
        )

        captured: list[MeshMessage] = []

        async def grab(message: MeshMessage) -> None:
            captured.append(message)

        # Observe through a second node's transport so the assertion also
        # proves the envelope verifies end-to-end.
        await fleet.transport("b").subscribe(HEARTBEAT_SUBJECT, grab)
        await pres_a._publish_heartbeat()
        await asyncio.sleep(0.02)

        assert captured, "no heartbeat captured"
        assert captured[-1].sender_id == "a"
        msg = json.loads(captured[-1].payload.decode("utf-8"))
        assert msg["schema_version"] == SCHEMA_VERSION
        assert msg["specs"]["cpu_percent"] == 10.0
        assert msg["specs"]["temp_celsius"] is None
        # Static fields ride along on every heartbeat too.
        assert msg["specs"]["model_name"] == "test-host"
        assert msg["specs"]["cpu_cores"] == 4


@pytest.mark.asyncio
class TestPresenceSecurity:
    """Issue #348 — signing, identity binding, replay, flood resistance."""

    async def test_unsigned_garbage_bytes_register_no_peer(self) -> None:
        fleet = Fleet(["a"])
        node_a = _make_node("a", "pi-alpha")
        pres_a = _presence(node_a, fleet, heartbeat_interval=10.0)
        await pres_a.start()
        try:
            # Raw legacy-style JSON (the pre-#348 wire format) and pure noise.
            legacy = json.dumps(_heartbeat_payload("evil", "pi-evil")).encode("utf-8")
            await fleet.bus.publish(HEARTBEAT_SUBJECT, legacy)
            await fleet.bus.publish(HEARTBEAT_SUBJECT, b"\x00\xffnot-json")
            assert node_a.peers == {}
            assert pres_a.rejected_count == 2
        finally:
            await pres_a.stop()

    async def test_heartbeat_signed_by_untrusted_key_dropped(self) -> None:
        fleet = Fleet(["a"])
        node_a = _make_node("a", "pi-alpha")
        pres_a = _presence(node_a, fleet, heartbeat_interval=10.0)
        await pres_a.start()
        try:
            attacker = fleet.untrusted_transport()
            await attacker.publish(
                _envelope("intruder", HEARTBEAT_SUBJECT, _heartbeat_payload("intruder"))
            )
            assert node_a.get_peer("intruder") is None
            assert node_a.peers == {}
            assert pres_a.rejected_count >= 1
        finally:
            await pres_a.stop()

    async def test_trusted_key_cannot_claim_other_sender_id_in_envelope(self) -> None:
        # Envelope sender_id "c" signed with B's key → SenderBindingError in
        # the transport; never reaches the presence handler.
        fleet = Fleet(["a", "b", "c"])
        node_a = _make_node("a", "pi-alpha")
        pres_a = _presence(node_a, fleet, heartbeat_interval=10.0)
        await pres_a.start()
        try:
            b_transport = fleet.transport("b")
            await b_transport.publish(_envelope("c", HEARTBEAT_SUBJECT, _heartbeat_payload("c")))
            assert node_a.peers == {}
            assert pres_a.rejected_count == 1
        finally:
            await pres_a.stop()

    async def test_payload_node_id_mismatch_rejected(self) -> None:
        # Valid envelope from trusted B, but the payload claims node_id "z":
        # rejected outright — never registered under the spoofed id.
        fleet = Fleet(["a", "b"])
        node_a = _make_node("a", "pi-alpha")
        pres_a = _presence(node_a, fleet, heartbeat_interval=10.0)
        await pres_a.start()
        try:
            b_transport = fleet.transport("b")
            await b_transport.publish(
                _envelope("b", HEARTBEAT_SUBJECT, _heartbeat_payload("z", "pi-spoof"))
            )
            assert node_a.get_peer("z") is None
            assert node_a.peers == {}
        finally:
            await pres_a.stop()

    async def test_leave_from_a_cannot_evict_b(self) -> None:
        fleet = Fleet(["me", "a", "b"])
        node_me = _make_node("me", "pi-alpha")
        pres_me = _presence(node_me, fleet, heartbeat_interval=10.0)
        await pres_me.start()
        try:
            # Register B via a valid signed heartbeat.
            await fleet.transport("b").publish(
                _envelope("b", HEARTBEAT_SUBJECT, _heartbeat_payload("b"))
            )
            assert node_me.get_peer("b") is not None

            # A publishes a leave whose payload targets B — B must stay.
            await fleet.transport("a").publish(
                _envelope("a", LEAVE_SUBJECT, {"node_id": "b", "ts_ms": _now_ms()})
            )
            assert node_me.get_peer("b") is not None
        finally:
            await pres_me.stop()

    async def test_valid_heartbeat_then_leave_from_same_sender(self) -> None:
        fleet = Fleet(["me", "b"])
        node_me = _make_node("me", "pi-alpha")
        pres_me = _presence(node_me, fleet, heartbeat_interval=10.0)
        await pres_me.start()
        try:
            b_transport = fleet.transport("b")
            await b_transport.publish(
                _envelope("b", HEARTBEAT_SUBJECT, _heartbeat_payload("b", "pi-beta"))
            )
            peer = node_me.get_peer("b")
            assert peer is not None
            assert peer.name == "pi-beta"

            await b_transport.publish(
                _envelope("b", LEAVE_SUBJECT, {"node_id": "b", "ts_ms": _now_ms()})
            )
            assert node_me.get_peer("b") is None
        finally:
            await pres_me.stop()

    async def test_replayed_envelope_ignored(self) -> None:
        fleet = Fleet(["me", "b"])
        node_me = _make_node("me", "pi-alpha")
        pres_me = _presence(node_me, fleet, heartbeat_interval=10.0)

        # Capture the exact signed frame B publishes so it can be replayed
        # byte-for-byte.
        frames: list[bytes] = []

        async def tap(raw: bytes) -> None:
            frames.append(raw)

        await fleet.bus.subscribe(HEARTBEAT_SUBJECT, tap)
        await pres_me.start()
        try:
            await fleet.transport("b").publish(
                _envelope("b", HEARTBEAT_SUBJECT, _heartbeat_payload("b"))
            )
            assert node_me.get_peer("b") is not None
            assert frames, "tap never saw the signed frame"

            # Evict B, then replay the captured frame: the replay window must
            # reject it, so B is NOT re-registered.
            node_me.remove_peer("b")
            before = pres_me.rejected_count
            await fleet.bus.publish(HEARTBEAT_SUBJECT, frames[-1])
            assert node_me.get_peer("b") is None
            assert pres_me.rejected_count == before + 1
        finally:
            await pres_me.stop()

    async def test_heartbeat_rate_limit_per_sender(self) -> None:
        fleet = Fleet(["me", "b"])
        node_me = _make_node("me", "pi-alpha")
        pres_me = _presence(node_me, fleet, heartbeat_interval=10.0, heartbeat_min_interval=10.0)
        await pres_me.start()
        try:
            b_transport = fleet.transport("b")
            await b_transport.publish(
                _envelope("b", HEARTBEAT_SUBJECT, _heartbeat_payload("b", "first"))
            )
            # Second heartbeat arrives immediately — under the 10s floor, so
            # its (changed) name must NOT be applied.
            await b_transport.publish(
                _envelope("b", HEARTBEAT_SUBJECT, _heartbeat_payload("b", "second"))
            )
            peer = node_me.get_peer("b")
            assert peer is not None
            assert peer.name == "first"
        finally:
            await pres_me.stop()


class TestPeerCap:
    def test_add_peer_capped_for_new_node_ids(self) -> None:
        node = MeshNode(SimpleNamespace(node_id="me", node_name="pi-alpha"))
        for i in range(MAX_PEERS):
            node.add_peer(PeerInfo(node_id=f"peer-{i}", name=f"p{i}"))
        assert len(node.peers) == MAX_PEERS

        # One past the cap: dropped.
        node.add_peer(PeerInfo(node_id="overflow", name="nope"))
        assert node.get_peer("overflow") is None
        assert len(node.peers) == MAX_PEERS

    def test_add_peer_updates_existing_at_cap(self) -> None:
        node = MeshNode(SimpleNamespace(node_id="me", node_name="pi-alpha"))
        for i in range(MAX_PEERS):
            node.add_peer(PeerInfo(node_id=f"peer-{i}", name=f"p{i}"))

        # Updating an already-known peer is always allowed.
        node.add_peer(PeerInfo(node_id="peer-0", name="renamed"))
        peer = node.get_peer("peer-0")
        assert peer is not None
        assert peer.name == "renamed"
        assert len(node.peers) == MAX_PEERS


@pytest.mark.asyncio
class TestPresenceAlertDispatcher:
    async def test_three_hot_heartbeats_emit_exactly_one_alert_frame(self) -> None:
        fleet = Fleet(["a", "b"])
        node_a = _make_node("a", "pi-alpha")
        node_b = _make_node("b", "pi-beta")

        frames: list[dict] = []

        async def stub_sink(frame: dict) -> None:
            frames.append(frame)

        dispatcher = AlertDispatcher(
            AlertEngine(now_ms=lambda: 1_700_000_000_000), send_frame=stub_sink
        )

        pres_a = _presence(node_a, fleet, heartbeat_interval=1.0)
        pres_b = _presence(
            node_b,
            fleet,
            heartbeat_interval=1.0,
            alert_dispatcher=dispatcher,
        )
        await pres_a.start()
        await pres_b.start()
        try:
            # Hand-publish three signed "hot" heartbeats *from* node_a so
            # pres_b's _on_heartbeat path feeds the dispatcher.
            a_transport = fleet.transport("a")
            hot_specs = _specs(temp_celsius=83.0)
            for _ in range(3):
                payload = _heartbeat_payload("a", "pi-alpha", specs=hot_specs.to_dict())
                await a_transport.publish(_envelope("a", HEARTBEAT_SUBJECT, payload))
                await asyncio.sleep(0.02)
        finally:
            await pres_a.stop()
            await pres_b.stop()

        # N_DANGER=2 → second hot heartbeat trips the engine; further hot
        # heartbeats while alerting emit no additional frames.
        alert_frames = [f for f in frames if f.get("type") == "alert"]
        assert len(alert_frames) == 1, f"expected 1 alert frame, got {alert_frames}"
        assert alert_frames[0]["state"] == "alerting"
        assert alert_frames[0]["node_id"] == "a"
        assert alert_frames[0]["severity"] == "danger"

    async def test_three_full_disk_heartbeats_emit_one_disk_alert_frame(self) -> None:
        """Test γ (DISK presence integration): heartbeats crossing DISK_DANGER
        drive a ``disk_pct`` alert frame through the presence → dispatcher path."""
        fleet = Fleet(["a", "b"])
        node_a = _make_node("a", "pi-alpha")
        node_b = _make_node("b", "pi-beta")

        frames: list[dict] = []

        async def stub_sink(frame: dict) -> None:
            frames.append(frame)

        dispatcher = AlertDispatcher(
            AlertEngine(now_ms=lambda: 1_700_000_000_000), send_frame=stub_sink
        )

        pres_a = _presence(node_a, fleet, heartbeat_interval=1.0)
        pres_b = _presence(
            node_b,
            fleet,
            heartbeat_interval=1.0,
            alert_dispatcher=dispatcher,
        )
        await pres_a.start()
        await pres_b.start()
        try:
            # 97 % of a 128 GiB disk → past DISK_DANGER (95 %).
            a_transport = fleet.transport("a")
            full_specs = _specs(disk_used_bytes=int(0.97 * 128 * 1024**3))
            for _ in range(3):
                payload = _heartbeat_payload("a", "pi-alpha", specs=full_specs.to_dict())
                await a_transport.publish(_envelope("a", HEARTBEAT_SUBJECT, payload))
                await asyncio.sleep(0.02)
        finally:
            await pres_a.stop()
            await pres_b.stop()

        disk_frames = [f for f in frames if f.get("field") == "disk_pct"]
        assert len(disk_frames) == 1, f"expected 1 disk_pct frame, got {disk_frames}"
        assert disk_frames[0]["state"] == "alerting"
        assert disk_frames[0]["severity"] == "danger"
        assert disk_frames[0]["node_id"] == "a"
