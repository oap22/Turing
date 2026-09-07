"""Model advertisement over presence heartbeats (peer model routing)."""

from __future__ import annotations

import asyncio
import json
import threading
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


def _node(
    node_id: str,
    name: str,
    advertise: str | None = None,
    allowed_hosts: list[str] | None = None,
) -> MeshNode:
    return MeshNode(
        SimpleNamespace(
            node_id=node_id,
            node_name=name,
            ollama_advertise_host=advertise,
            ollama_peer_allowlist=allowed_hosts
            if allowed_hosts is not None
            else ([advertise] if advertise else []),
        )
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


async def _wait_for_thread_event(event: threading.Event) -> None:
    """Wait without blocking the owning asyncio loop for a worker signal."""
    for _ in range(100):
        if event.is_set():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("worker event was not set")


class TestModelAdvertisement:
    async def test_models_and_host_round_trip_via_heartbeat(self) -> None:
        fleet = _Fleet(["a", "b"])
        node_a = _node("a", "jetson-1", advertise="http://jetson-1:11434")
        node_b = _node("b", "jetson-2", allowed_hosts=["http://jetson-1:11434"])
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

    async def test_slow_sampler_does_not_block_start_and_publishes_last_known_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Presence startup bounds optional Ollama discovery and keeps metadata safe."""
        fleet = _Fleet(["a", "b"])
        node_a = _node("a", "jetson-1", advertise="http://jetson-1:11434")
        node_b = _node("b", "jetson-2", allowed_hosts=["http://jetson-1:11434"])
        pres_a = _presence(node_a, fleet)
        pres_b = _presence(node_b, fleet)
        monkeypatch.setattr("turing.mesh.presence.MODEL_SAMPLE_TIMEOUT", 0.01)
        sampler_release = threading.Event()
        sampler_finished = threading.Event()

        async def slow_failing_sampler() -> list[str]:
            while not sampler_release.is_set():
                await asyncio.sleep(0.005)
            sampler_finished.set()
            raise ConnectionError("ollama unreachable")

        pres_a.set_model_sampler(slow_failing_sampler)
        await pres_b.start()
        await asyncio.wait_for(pres_a.start(), timeout=0.2)
        try:
            # The timed-out optional probe publishes a safe empty list rather
            # than delaying the real heartbeat/startup path.
            peer = node_b.get_peer("a")
            assert peer is not None
            assert peer.models == []
            assert node_a.self_models == []

            # Once a model list exists, the same timeout/failure preserves it
            # in the next heartbeat instead of advertising a false empty list.
            node_a.self_models = ["qwen2.5:7b"]
            pres_a._models_sampled_at = None
            await pres_a._publish_heartbeat()
            peer = node_b.get_peer("a")
            assert peer is not None
            assert peer.models == ["qwen2.5:7b"]
        finally:
            await pres_a.stop()
            await pres_b.stop()
            sampler_release.set()
            await _wait_for_thread_event(sampler_finished)

    async def test_cancellation_resistant_sampler_cannot_block_stop_or_publish_late_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Detached probes suppress late results and exceptions safely."""
        fleet = _Fleet(["a", "b"])
        node_a = _node("a", "jetson-1", advertise="http://jetson-1:11434")
        node_b = _node("b", "jetson-2", allowed_hosts=["http://jetson-1:11434"])
        pres_a = _presence(node_a, fleet)
        pres_b = _presence(node_b, fleet)
        monkeypatch.setattr("turing.mesh.presence.MODEL_SAMPLE_TIMEOUT", 0.01)
        sampler_started = threading.Event()
        sampler_release = threading.Event()
        sampler_finished = threading.Event()

        async def cancellation_resistant_sampler() -> list[str]:
            sampler_started.set()
            try:
                while not sampler_release.is_set():
                    await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                while not sampler_release.is_set():
                    await asyncio.sleep(0.005)
            sampler_finished.set()
            raise RuntimeError("late sampler failure") from None

        pres_a.set_model_sampler(cancellation_resistant_sampler)
        await pres_b.start()
        try:
            await asyncio.wait_for(pres_a.start(), timeout=0.2)
            await _wait_for_thread_event(sampler_started)
            sample_handle = pres_a._model_sample
            assert sample_handle is not None and not sample_handle.completed
            peer = node_b.get_peer("a")
            assert peer is not None
            assert peer.models == []

            # A late result cannot replace a cached model list after timeout.
            node_a.self_models = ["previous-model"]
            pres_a._models_sampled_at = None
            await asyncio.wait_for(pres_a._publish_heartbeat(), timeout=0.2)
            peer = node_b.get_peer("a")
            assert peer is not None
            assert peer.models == ["previous-model"]

            # stop() detaches without waiting for cancellation-resistant code.
            started = time.monotonic()
            await asyncio.wait_for(pres_a.stop(), timeout=0.2)
            assert time.monotonic() - started < 0.2
            sampler_release.set()
            await _wait_for_thread_event(sampler_finished)
            for _ in range(100):
                if sample_handle.completed:
                    break
                await asyncio.sleep(0.005)
            assert sample_handle.completed
            assert isinstance(sample_handle.error, RuntimeError)
            assert node_a.self_models == ["previous-model"]
        finally:
            sampler_release.set()
            await _wait_for_thread_event(sampler_finished)
            if pres_a.is_running:
                await pres_a.stop()
            await pres_b.stop()

    async def test_cancellation_resistant_late_result_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A detached successful probe cannot overwrite cached publication."""
        fleet = _Fleet(["a", "b"])
        node_a = _node("a", "jetson-1", advertise="http://jetson-1:11434")
        node_b = _node("b", "jetson-2", allowed_hosts=["http://jetson-1:11434"])
        pres_a = _presence(node_a, fleet)
        pres_b = _presence(node_b, fleet)
        monkeypatch.setattr("turing.mesh.presence.MODEL_SAMPLE_TIMEOUT", 0.01)
        sampler_started = threading.Event()
        sampler_release = threading.Event()
        sampler_finished = threading.Event()

        async def late_sampler() -> list[str]:
            sampler_started.set()
            try:
                while not sampler_release.is_set():
                    await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                while not sampler_release.is_set():
                    await asyncio.sleep(0.005)
            sampler_finished.set()
            return ["late-model"]

        pres_a.set_model_sampler(late_sampler)
        await pres_b.start()
        try:
            await pres_a.start()
            await _wait_for_thread_event(sampler_started)
            sample_handle = pres_a._model_sample
            assert sample_handle is not None and not sample_handle.completed
            node_a.self_models = ["previous-model"]
            pres_a._models_sampled_at = None
            await asyncio.wait_for(pres_a._publish_heartbeat(), timeout=0.2)
            peer = node_b.get_peer("a")
            assert peer is not None
            assert peer.models == ["previous-model"]

            started = time.monotonic()
            await asyncio.wait_for(pres_a.stop(), timeout=0.2)
            assert time.monotonic() - started < 0.2
            sampler_release.set()
            await _wait_for_thread_event(sampler_finished)
            for _ in range(100):
                if sample_handle.completed:
                    break
                await asyncio.sleep(0.005)
            assert sample_handle.completed
            assert sample_handle.result == ["late-model"]
            assert node_a.self_models == ["previous-model"]
        finally:
            sampler_release.set()
            await _wait_for_thread_event(sampler_finished)
            if pres_a.is_running:
                await pres_a.stop()
            await pres_b.stop()

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
