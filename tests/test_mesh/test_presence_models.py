"""Model advertisement over presence heartbeats (peer model routing)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from turing.mesh.node import MeshNode
from turing.mesh.presence import (
    HEARTBEAT_SUBJECT,
    MODEL_SAMPLE_INTERVAL,
    SCHEMA_VERSION,
    PresenceService,
)
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner

pytestmark = pytest.mark.asyncio


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Fleet:
    def __init__(self, node_ids: list[str]) -> None:
        self.bus = InMemoryBus()
        self.signers = {nid: MessageSigner.generate() for nid in node_ids}
        self.trusted = {nid: s.public_key for nid, s in self.signers.items()}

    def transport(self, node_id: str) -> SignedTransport:
        return SignedTransport(
            bus=self.bus,
            signer=self.signers[node_id],
            trusted_keys=self.trusted,
            now_ms=_now_ms,
        )


def _node(node_id: str, name: str, advertise: str | None = None) -> MeshNode:
    return MeshNode(
        SimpleNamespace(node_id=node_id, node_name=name, ollama_advertise_host=advertise)
    )


def _presence(node: MeshNode, fleet: _Fleet) -> PresenceService:
    pres = PresenceService(
        node,
        fleet.transport(node.node_id),
        heartbeat_interval=0.05,
        stale_after=60.0,
        heartbeat_min_interval=0.0,
    )
    pres._sample_self_specs = lambda: None  # type: ignore[method-assign]
    return pres


async def _wait_for_peer(node: MeshNode, peer_id: str) -> Any:
    for _ in range(100):
        await asyncio.sleep(0.02)
        peer = node.get_peer(peer_id)
        if peer is not None and peer.models:
            return peer
    return node.get_peer(peer_id)


class TestModelAdvertisement:
    async def test_models_and_host_round_trip_via_heartbeat(self) -> None:
        fleet = _Fleet(["a", "b"])
        node_a = _node("a", "jetson-1", advertise="http://jetson-1:11434")
        node_b = _node("b", "jetson-2")
        pres_a = _presence(node_a, fleet)
        pres_b = _presence(node_b, fleet)
        pres_a.set_model_sampler(AsyncMock(return_value=["qwen2.5:7b", "gemma3:1b", "qwen2.5:7b"]))

        await pres_a.start()
        await pres_b.start()
        try:
            peer = await _wait_for_peer(node_b, "a")
            assert peer is not None
            # De-duplicated and sorted so the heartbeat is byte-stable.
            assert peer.models == ["gemma3:1b", "qwen2.5:7b"]
            assert peer.ollama_host == "http://jetson-1:11434"
            assert node_a.self_models == ["gemma3:1b", "qwen2.5:7b"]
        finally:
            await pres_a.stop()
            await pres_b.stop()

    async def test_unadvertised_host_is_none_for_peers(self) -> None:
        # ollama_advertise_host unset → peers must never learn a host to
        # target (a localhost URL would point at themselves).
        fleet = _Fleet(["a", "b"])
        node_a = _node("a", "jetson-1")
        node_b = _node("b", "jetson-2")
        pres_a = _presence(node_a, fleet)
        pres_b = _presence(node_b, fleet)
        pres_a.set_model_sampler(AsyncMock(return_value=["gemma3:1b"]))
        await pres_a.start()
        await pres_b.start()
        try:
            peer = await _wait_for_peer(node_b, "a")
            assert peer is not None
            assert peer.models == ["gemma3:1b"]
            assert peer.ollama_host is None
        finally:
            await pres_a.stop()
            await pres_b.stop()

    async def test_sampler_is_cached_between_heartbeats(self) -> None:
        fleet = _Fleet(["a"])
        node_a = _node("a", "jetson-1")
        pres_a = _presence(node_a, fleet)
        sampler = AsyncMock(return_value=["gemma3:1b"])
        pres_a.set_model_sampler(sampler)

        first = await pres_a._sample_self_models()
        second = await pres_a._sample_self_models()
        assert first == second == ["gemma3:1b"]
        assert sampler.await_count == 1, "second call within the interval must hit the cache"

        # Expire the cache and confirm a resample happens.
        pres_a._models_sampled_at = time.monotonic() - MODEL_SAMPLE_INTERVAL - 1
        sampler.return_value = ["gemma3:1b", "qwen2.5:7b"]
        assert await pres_a._sample_self_models() == ["gemma3:1b", "qwen2.5:7b"]
        assert sampler.await_count == 2

    async def test_sampler_failure_keeps_last_known_list(self) -> None:
        fleet = _Fleet(["a"])
        node_a = _node("a", "jetson-1")
        pres_a = _presence(node_a, fleet)
        sampler = AsyncMock(return_value=["gemma3:1b"])
        pres_a.set_model_sampler(sampler)
        assert await pres_a._sample_self_models() == ["gemma3:1b"]

        pres_a._models_sampled_at = None
        sampler.side_effect = ConnectionError("ollama restarting")
        assert await pres_a._sample_self_models() == ["gemma3:1b"]

    async def test_no_sampler_means_empty_models(self) -> None:
        fleet = _Fleet(["a"])
        pres_a = _presence(_node("a", "jetson-1"), fleet)
        assert await pres_a._sample_self_models() == []

    async def test_garbage_model_fields_are_ignored(self) -> None:
        # A trusted peer on a build with odd payloads must not crash the
        # subscriber or poison the peer table with non-string models.
        fleet = _Fleet(["a", "b"])
        node_b = _node("b", "jetson-2")
        pres_b = _presence(node_b, fleet)
        await pres_b.start()
        try:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "node_id": "a",
                "node_name": "jetson-1",
                "capabilities": [],
                "ts_ms": _now_ms(),
                "specs": None,
                "models": ["ok", 7, None, {"x": 1}],
                "ollama_host": 42,
            }
            await fleet.transport("a").publish(
                MeshMessage(
                    request_id=uuid.uuid4().hex,
                    sender_id="a",
                    subject=HEARTBEAT_SUBJECT,
                    payload=json.dumps(payload).encode("utf-8"),
                    timestamp_ms=_now_ms(),
                )
            )
            for _ in range(50):
                await asyncio.sleep(0.02)
                if node_b.get_peer("a") is not None:
                    break
            peer = node_b.get_peer("a")
            assert peer is not None
            assert peer.models == ["ok"]
            assert peer.ollama_host is None
        finally:
            await pres_b.stop()
