"""Tests for the mesh-aware local tier (turing.llm.pool)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from turing.llm.base import LLMProvider, LLMResponse, Message, Role
from turing.llm.pool import LOCAL_MODEL_CACHE_TTL, PeerModelPool, peer_load, select_peer
from turing.mesh.node import PeerInfo
from turing.specs.collector import NodeSpecs

MODEL = "qwen2.5:7b"
HOST = "http://host:11434"


def _specs(loadavg_1m: float, cores: int = 4) -> NodeSpecs:
    return NodeSpecs(
        model_name="jetson",
        os="linux",
        arch="aarch64",
        cpu_cores=cores,
        ram_total_bytes=8 * 1024**3,
        disk_total_bytes=64 * 1024**3,
        cpu_percent=0.0,
        mem_used_bytes=0,
        disk_used_bytes=0,
        temp_celsius=None,
        uptime_seconds=0,
        loadavg_1m=loadavg_1m,
        loadavg_5m=0.0,
        loadavg_15m=0.0,
    )


def _peer(
    name: str,
    models: list[str] | None = None,
    host: str | None = "http://host:11434",
    load: float | None = None,
    stale: bool = False,
) -> PeerInfo:
    peer = PeerInfo(
        node_id=name,
        name=name,
        models=models if models is not None else [MODEL],
        ollama_host=host,
        specs=_specs(load) if load is not None else None,
    )
    if stale:
        peer.last_seen = time.time() - 120
    return peer


def _provider(content: str) -> AsyncMock:
    p = AsyncMock(spec=LLMProvider)
    p.complete = AsyncMock(return_value=LLMResponse(content=content, model=MODEL))
    return p


class TestSelectPeer:
    def test_no_candidates_when_nobody_has_the_model(self) -> None:
        assert select_peer([_peer("a", models=["other"])], MODEL) is None

    def test_skips_peers_without_an_advertised_host(self) -> None:
        # A peer that has the model but never set ollama_advertise_host is
        # unreachable by construction; choosing it would just fail.
        assert select_peer([_peer("a", host=None)], MODEL) is None

    def test_skips_stale_peers(self) -> None:
        assert select_peer([_peer("a", stale=True)], MODEL) is None

    def test_prefers_lowest_load_per_core(self) -> None:
        busy = _peer("busy", load=3.0)
        idle = _peer("idle", load=0.4)
        assert select_peer([busy, idle], MODEL, allowed_hosts=[HOST]) is idle

    def test_unknown_load_sorts_after_measured(self) -> None:
        unknown = _peer("unknown")  # no specs
        measured = _peer("measured", load=2.0)  # 0.5 per core < 1.0 pessimistic
        assert select_peer([unknown, measured], MODEL, allowed_hosts=[HOST]) is measured
        assert peer_load(unknown) == 1.0

    def test_tie_breaks_on_name_for_determinism(self) -> None:
        a = _peer("a", load=1.0)
        b = _peer("b", load=1.0)
        assert select_peer([b, a], MODEL, allowed_hosts=[HOST]) is a

    def test_skips_peer_outside_exact_allowlist(self) -> None:
        assert select_peer([_peer("a", host="http://other:11434")], MODEL, allowed_hosts=[HOST]) is None


class TestPeerModelPool:
    @pytest.mark.asyncio
    async def test_serves_locally_until_local_models_are_known(self) -> None:
        local = _provider("local")
        factory = AsyncMock()
        pool = PeerModelPool(
            local,
            model=MODEL,
            provider_factory=factory,
            allowed_peer_hosts=["http://a:11434"],
        )
        pool.attach_peers(lambda: [_peer("a")])

        result = await pool.complete([Message(role=Role.USER, content="hi")])

        assert result.content == "local"
        factory.assert_not_called()
        assert pool.describe()["target"] == "local"

    @pytest.mark.asyncio
    async def test_serves_locally_when_local_has_the_model(self) -> None:
        local = _provider("local")
        local.list_models = AsyncMock(return_value=[MODEL, "gemma3:1b"])
        pool = PeerModelPool(local, model=MODEL, provider_factory=AsyncMock())
        pool.attach_peers(lambda: [_peer("a")])

        assert await pool.refresh_local_models() is True
        result = await pool.complete([Message(role=Role.USER, content="hi")])
        assert result.content == "local"

    @pytest.mark.asyncio
    async def test_routes_to_peer_when_local_lacks_the_model(self) -> None:
        local = _provider("local")
        local.list_models = AsyncMock(return_value=["gemma3:1b"])
        remote = _provider("remote")
        built: list[tuple[str, str]] = []

        def factory(host: str, model: str) -> LLMProvider:
            built.append((host, model))
            return remote

        pool = PeerModelPool(
            local,
            model=MODEL,
            provider_factory=factory,
            allowed_peer_hosts=["http://a:11434"],
        )
        pool.attach_peers(lambda: [_peer("a", host="http://a:11434")])
        await pool.refresh_local_models()

        first = await pool.complete([Message(role=Role.USER, content="hi")])
        second = await pool.complete([Message(role=Role.USER, content="again")])

        assert first.content == "remote" and second.content == "remote"
        # One HTTP client per peer host, not one per turn.
        assert built == [("http://a:11434", MODEL)]
        local.complete.assert_not_called()
        assert pool.describe()["target"] == "peer:a"

    @pytest.mark.asyncio
    async def test_falls_back_to_local_when_no_peer_has_it(self) -> None:
        local = _provider("local")
        local.list_models = AsyncMock(return_value=[])
        pool = PeerModelPool(local, model=MODEL, provider_factory=AsyncMock())
        pool.attach_peers(lambda: [_peer("a", models=["other"])])
        await pool.refresh_local_models()

        result = await pool.complete([Message(role=Role.USER, content="hi")])
        assert result.content == "local"
        assert pool.describe()["target"] == "local:no-healthy-peer-has-model"

    @pytest.mark.asyncio
    async def test_peer_failure_retries_locally_once(self) -> None:
        local = _provider("local")
        local.list_models = AsyncMock(return_value=[])
        remote = _provider("remote")
        remote.complete = AsyncMock(side_effect=ConnectionError("peer down"))
        pool = PeerModelPool(
            local,
            model=MODEL,
            provider_factory=lambda _h, _m: remote,
            allowed_peer_hosts=[HOST],
        )
        pool.attach_peers(lambda: [_peer("a")])
        await pool.refresh_local_models()

        result = await pool.complete([Message(role=Role.USER, content="hi")])

        assert result.content == "local"
        remote.complete.assert_awaited_once()
        local.complete.assert_awaited_once()

        # A live Turing heartbeat may keep advertising a dead Ollama. The
        # next request must not pay that endpoint's timeout again.
        await pool.complete([Message(role=Role.USER, content="again")])
        remote.complete.assert_awaited_once()
        assert local.complete.await_count == 2
        assert pool.describe()["quarantined_hosts"] == ["http://host:11434"]

    @pytest.mark.asyncio
    async def test_peer_failure_tries_another_peer_before_local(self) -> None:
        local = _provider("local")
        local.list_models = AsyncMock(return_value=[])
        dead = _provider("dead")
        dead.complete = AsyncMock(side_effect=ConnectionError("down"))
        healthy = _provider("healthy")
        providers = {"http://a:11434": dead, "http://b:11434": healthy}
        pool = PeerModelPool(
            local,
            model=MODEL,
            provider_factory=lambda host, _m: providers[host],
            allowed_peer_hosts=["http://a:11434", "http://b:11434"],
        )
        pool.attach_peers(
            lambda: [
                _peer("a", host="http://a:11434", load=0.1),
                _peer("b", host="http://b:11434", load=0.2),
            ]
        )
        await pool.refresh_local_models()

        result = await pool.complete([Message(role=Role.USER, content="hi")])

        assert result.content == "healthy"
        dead.complete.assert_awaited_once()
        healthy.complete.assert_awaited_once()
        local.complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_provider_factory_failure_quarantines_and_tries_next_peer(self) -> None:
        local = _provider("local")
        local.list_models = AsyncMock(return_value=[])
        healthy = _provider("healthy")

        def factory(host: str, _model: str) -> LLMProvider:
            if host == "http://a:11434":
                raise ValueError("malformed peer endpoint")
            return healthy

        pool = PeerModelPool(
            local,
            model=MODEL,
            provider_factory=factory,
            allowed_peer_hosts=["http://a:11434", "http://b:11434"],
        )
        pool.attach_peers(
            lambda: [
                _peer("a", host="http://a:11434", load=0.1),
                _peer("b", host="http://b:11434", load=0.2),
            ]
        )
        await pool.refresh_local_models()

        result = await pool.complete([Message(role=Role.USER, content="hi")])

        assert result.content == "healthy"
        assert pool.describe()["quarantined_hosts"] == ["http://a:11434"]

    @pytest.mark.asyncio
    async def test_local_model_cache_refreshes_after_ttl_or_invalidation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local = _provider("local")
        local.list_models = AsyncMock(return_value=[MODEL])
        pool = PeerModelPool(local, model=MODEL, provider_factory=AsyncMock())
        clock = 100.0
        monkeypatch.setattr("turing.llm.pool.time.monotonic", lambda: clock)

        assert await pool.refresh_local_models() is True
        assert await pool.refresh_local_models() is True
        assert local.list_models.await_count == 1
        clock += LOCAL_MODEL_CACHE_TTL
        assert await pool.refresh_local_models() is True
        assert local.list_models.await_count == 2
        pool.invalidate_local_model_cache()
        assert await pool.refresh_local_models() is True
        assert local.list_models.await_count == 3

    @pytest.mark.asyncio
    async def test_health_status_reports_local_and_selected_peer_separately(self) -> None:
        local = _provider("local")
        local.list_models = AsyncMock(return_value=[])
        local.health_check = AsyncMock(return_value=True)
        remote = _provider("remote")
        remote.health_check = AsyncMock(return_value=True)
        pool = PeerModelPool(
            local,
            model=MODEL,
            provider_factory=lambda _host, _model: remote,
            allowed_peer_hosts=[HOST],
        )
        pool.attach_peers(lambda: [_peer("a")])
        await pool.refresh_local_models()

        status = await pool.health_status()

        assert status["local"] == {"target": "local", "healthy": True}
        assert status["peer"] == {"host": HOST, "healthy": True}
        assert status["selected"] == {"target": "peer:a", "healthy": True}

    @pytest.mark.asyncio
    async def test_stream_peer_failure_before_output_falls_back_local(self) -> None:
        local = _provider("local")
        local.list_models = AsyncMock(return_value=[])
        remote = _provider("remote")

        async def failed_stream(*_args: object, **_kwargs: object):
            if False:
                yield ""
            raise ConnectionError("peer down")

        async def local_stream(*_args: object, **_kwargs: object):
            yield "local"

        remote.stream = failed_stream
        local.stream = local_stream
        pool = PeerModelPool(
            local,
            model=MODEL,
            provider_factory=lambda _h, _m: remote,
            allowed_peer_hosts=[HOST],
        )
        pool.attach_peers(lambda: [_peer("a")])
        await pool.refresh_local_models()

        assert [part async for part in pool.stream([])] == ["local"]

    @pytest.mark.asyncio
    async def test_stream_failure_after_output_does_not_mix_models(self) -> None:
        local = _provider("local")
        local.list_models = AsyncMock(return_value=[])
        remote = _provider("remote")

        async def partial_stream(*_args: object, **_kwargs: object):
            yield "peer"
            raise ConnectionError("peer died mid-stream")

        async def local_stream(*_args: object, **_kwargs: object):
            yield "local"

        remote.stream = partial_stream
        local.stream = local_stream
        pool = PeerModelPool(
            local,
            model=MODEL,
            provider_factory=lambda _h, _m: remote,
            allowed_peer_hosts=[HOST],
        )
        pool.attach_peers(lambda: [_peer("a")])
        await pool.refresh_local_models()

        stream = pool.stream([])
        assert await anext(stream) == "peer"
        with pytest.raises(ConnectionError):
            await anext(stream)

    @pytest.mark.asyncio
    async def test_local_failure_propagates_for_the_router(self) -> None:
        # The router owns local→cloud fallback; the pool must not swallow it.
        local = _provider("local")
        local.complete = AsyncMock(side_effect=RuntimeError("ollama down"))
        pool = PeerModelPool(local, model=MODEL, provider_factory=AsyncMock())
        with pytest.raises(RuntimeError):
            await pool.complete([Message(role=Role.USER, content="hi")])

    @pytest.mark.asyncio
    async def test_model_list_failure_leaves_cache_unknown(self) -> None:
        local = _provider("local")
        local.list_models = AsyncMock(side_effect=ConnectionError("no ollama"))
        pool = PeerModelPool(local, model=MODEL, provider_factory=AsyncMock())
        assert await pool.refresh_local_models() is None
        assert pool.local_has_model is None
        # Unknown means "stay on-box".
        assert pool.select()[1] == "local"

    @pytest.mark.asyncio
    async def test_provider_without_list_models_is_tolerated(self) -> None:
        local = AsyncMock(spec=LLMProvider)  # spec'd: no list_models attribute
        pool = PeerModelPool(local, model=MODEL, provider_factory=AsyncMock())
        assert await pool.refresh_local_models() is None

    @pytest.mark.asyncio
    async def test_health_check_delegates_to_local(self) -> None:
        local = AsyncMock(spec=LLMProvider)
        local.health_check = AsyncMock(return_value=True)
        pool = PeerModelPool(local, model=MODEL, provider_factory=AsyncMock())
        assert await pool.health_check() is True
