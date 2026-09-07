"""Model advertisement over presence heartbeats (peer model routing)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest

from turing.mesh.node import MeshNode
from turing.mesh.presence import (
    HEARTBEAT_SUBJECT,
    MODEL_SAMPLE_INTERVAL,
    SCHEMA_VERSION,
    ModelSamplerContractError,
    PresenceService,
    background_loop_safe_model_sampler,
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
        pres_a.set_model_sampler(
            background_loop_safe_model_sampler(
                AsyncMock(return_value=["qwen2.5:7b", "gemma3:1b", "qwen2.5:7b"])
            )
        )

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
        pres_a.set_model_sampler(
            background_loop_safe_model_sampler(AsyncMock(return_value=["gemma3:1b"]))
        )
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
        pres_a.set_model_sampler(background_loop_safe_model_sampler(sampler))

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
        pres_a.set_model_sampler(background_loop_safe_model_sampler(sampler))
        assert await pres_a._sample_self_models() == ["gemma3:1b"]

        pres_a._models_sampled_at = None
        sampler.side_effect = ConnectionError("ollama restarting")
        assert await pres_a._sample_self_models() == ["gemma3:1b"]

    async def test_unmarked_sampler_is_rejected(self) -> None:
        fleet = _Fleet(["a"])
        pres_a = _presence(_node("a", "jetson-1"), fleet)

        async def unmarked_sampler() -> list[str]:
            return []

        with pytest.raises(ModelSamplerContractError, match="opt in"):
            pres_a.set_model_sampler(unmarked_sampler)

    async def test_ollama_sampler_uses_a_dedicated_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Presence must not reuse the provider's request-loop-bound client."""
        import ollama

        from turing.llm.local import OllamaProvider

        fleet = _Fleet(["a"])
        pres_a = _presence(_node("a", "jetson-1"), fleet)
        provider = OllamaProvider(host="http://ollama.test")
        shared_list = AsyncMock(side_effect=AssertionError("shared client was reused"))
        provider._client.list = shared_list  # type: ignore[method-assign]

        class DedicatedClient:
            instances: ClassVar[list[DedicatedClient]] = []

            def __init__(self, *, host: str, timeout: Any) -> None:
                self.host = host
                self.timeout = timeout
                self.closed = False
                self.instances.append(self)

            async def list(self) -> dict[str, list[dict[str, str]]]:
                return {"models": [{"name": "dedicated-model"}]}

            async def close(self) -> None:
                self.closed = True

        monkeypatch.setattr(ollama, "AsyncClient", DedicatedClient)
        pres_a.set_model_sampler(provider.list_models)
        assert await pres_a._sample_self_models() == ["dedicated-model"]
        assert shared_list.await_count == 0
        assert len(DedicatedClient.instances) == 1
        assert DedicatedClient.instances[0].host == "http://ollama.test"
        assert DedicatedClient.instances[0].closed

    async def test_loop_affine_sampler_fails_clearly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sampler awaiting an app-loop Future is rejected, not silently accepted."""
        fleet = _Fleet(["a"])
        node_a = _node("a", "jetson-1")
        pres_a = _presence(node_a, fleet)
        app_loop = asyncio.get_running_loop()
        app_future = app_loop.create_future()
        contract_errors: list[tuple[str, dict[str, Any]]] = []

        async def loop_affine_sampler() -> list[str]:
            await app_future
            return ["never-advertised"]

        monkeypatch.setattr(
            "turing.mesh.presence.logger.error",
            lambda event, **fields: contract_errors.append((event, fields)),
        )
        pres_a.set_model_sampler(background_loop_safe_model_sampler(loop_affine_sampler))
        assert await pres_a._sample_self_models() == []
        assert contract_errors
        event, fields = contract_errors[0]
        assert event == "presence_model_sampler_contract_violation"
        assert "application's event loop" in str(fields)
        assert pres_a._active_model_sample is None

    async def test_sampler_replacement_is_rejected_while_detached_worker_lives(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unkillable old sampler cannot be replaced by another live worker."""
        fleet = _Fleet(["a"])
        pres_a = _presence(_node("a", "jetson-1"), fleet)
        monkeypatch.setattr("turing.mesh.presence.MODEL_SAMPLE_TIMEOUT", 0.01)
        sampler_started = threading.Event()
        sampler_release = threading.Event()
        sampler_finished = threading.Event()

        async def cancellation_resistant_sampler() -> list[str]:
            sampler_started.set()
            while not sampler_release.is_set():
                await asyncio.sleep(0.005)
            sampler_finished.set()
            return ["late-model"]

        pres_a.set_model_sampler(background_loop_safe_model_sampler(cancellation_resistant_sampler))
        try:
            assert await pres_a._sample_self_models() == []
            await _wait_for_thread_event(sampler_started)
            handle = pres_a._active_model_sample
            assert handle is not None and not handle.completed
            with pytest.raises(RuntimeError, match="still active"):
                pres_a.set_model_sampler(
                    background_loop_safe_model_sampler(AsyncMock(return_value=["replacement"]))
                )

            sampler_release.set()
            await _wait_for_thread_event(sampler_finished)
            for _ in range(100):
                if handle.completed:
                    break
                await asyncio.sleep(0.005)
            assert handle.completed
            pres_a.set_model_sampler(
                background_loop_safe_model_sampler(AsyncMock(return_value=["replacement"]))
            )
            assert await pres_a._sample_self_models() == ["replacement"]
        finally:
            sampler_release.set()
            await _wait_for_thread_event(sampler_finished)
            if pres_a.is_running:
                await pres_a.stop()

    async def test_stale_waiter_cannot_clear_or_apply_new_generation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Replacement or disable wins over a completed but stale waiter."""
        fleet = _Fleet(["a", "b"])
        result_service = _presence(_node("a", "jetson-1"), fleet)
        error_service = _presence(_node("b", "jetson-2"), fleet)
        result_gate = asyncio.Event()
        error_gate = asyncio.Event()
        result_wait_started = asyncio.Event()
        error_wait_started = asyncio.Event()
        active_gate = result_gate
        active_wait_started = result_wait_started

        async def blocked_wait(_handle: Any) -> None:
            active_wait_started.set()
            await active_gate.wait()

        monkeypatch.setattr("turing.mesh.presence._DetachedModelSampler.wait", blocked_wait)
        result_release = threading.Event()
        result_finished = threading.Event()
        error_release = threading.Event()
        error_finished = threading.Event()

        async def late_result() -> list[str]:
            while not result_release.is_set():
                await asyncio.sleep(0.005)
            result_finished.set()
            return ["stale-result"]

        async def late_error() -> list[str]:
            while not error_release.is_set():
                await asyncio.sleep(0.005)
            error_finished.set()
            raise RuntimeError("stale sampler failure")

        result_service.set_model_sampler(background_loop_safe_model_sampler(late_result))
        result_task = asyncio.create_task(result_service._sample_self_models())
        await result_wait_started.wait()
        result_handle = result_service._active_model_sample
        assert result_handle is not None
        result_release.set()
        await _wait_for_thread_event(result_finished)
        for _ in range(100):
            if result_handle.completed:
                break
            await asyncio.sleep(0.005)
        assert result_handle.completed

        replacement = background_loop_safe_model_sampler(AsyncMock(return_value=["replacement"]))
        result_service.set_model_sampler(replacement)
        result_gate.set()
        assert await result_task == []
        assert result_service._model_sampler is replacement
        assert result_service._model_sample is None
        assert result_service._active_model_sample is None
        assert result_service._node.self_models == []

        active_gate = error_gate
        active_wait_started = error_wait_started
        error_service.set_model_sampler(background_loop_safe_model_sampler(late_error))
        error_task = asyncio.create_task(error_service._sample_self_models())
        await error_wait_started.wait()
        error_handle = error_service._active_model_sample
        assert error_handle is not None
        error_release.set()
        await _wait_for_thread_event(error_finished)
        for _ in range(100):
            if error_handle.completed:
                break
            await asyncio.sleep(0.005)
        assert error_handle.completed

        error_service._node.self_models = ["previous-model"]
        error_service.set_model_sampler(None)
        error_gate.set()
        assert await error_task == ["previous-model"]
        assert isinstance(error_handle.error, RuntimeError)
        assert error_service._model_sample is None
        assert error_service._active_model_sample is None
        assert error_service._node.self_models == ["previous-model"]

    async def test_restart_does_not_duplicate_an_unfinished_detached_sampler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stopped service retains one live sampler until it can be reaped."""
        fleet = _Fleet(["a"])
        pres_a = _presence(_node("a", "jetson-1"), fleet)
        pres_a._heartbeat_interval = 60.0
        monkeypatch.setattr("turing.mesh.presence.MODEL_SAMPLE_TIMEOUT", 0.01)
        sampler_release = threading.Event()
        sampler_started = threading.Event()
        sampler_finished = threading.Event()
        started_count = 0

        async def sampler() -> list[str]:
            nonlocal started_count
            started_count += 1
            if started_count == 1:
                sampler_started.set()
                while not sampler_release.is_set():
                    await asyncio.sleep(0.005)
                sampler_finished.set()
            return [f"model-{started_count}"]

        pres_a.set_model_sampler(background_loop_safe_model_sampler(sampler))
        try:
            await pres_a.start()
            await _wait_for_thread_event(sampler_started)
            first_handle = pres_a._active_model_sample
            assert first_handle is not None and not first_handle.completed

            await pres_a.stop()
            await pres_a.start()
            await asyncio.sleep(0.05)
            assert started_count == 1
            assert pres_a._active_model_sample is first_handle

            sampler_release.set()
            await _wait_for_thread_event(sampler_finished)
            for _ in range(100):
                if first_handle.completed:
                    break
                await asyncio.sleep(0.005)
            assert first_handle.completed

            pres_a._models_sampled_at = None
            assert await pres_a._sample_self_models() == ["model-2"]
            assert started_count == 2
        finally:
            sampler_release.set()
            await _wait_for_thread_event(sampler_finished)
            if pres_a.is_running:
                await pres_a.stop()

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

        pres_a.set_model_sampler(background_loop_safe_model_sampler(slow_failing_sampler))
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

        pres_a.set_model_sampler(background_loop_safe_model_sampler(cancellation_resistant_sampler))
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

        pres_a.set_model_sampler(background_loop_safe_model_sampler(late_sampler))
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
