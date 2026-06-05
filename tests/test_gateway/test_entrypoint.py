"""Standalone gateway entrypoint — ``python -m turing.gateway`` (ADR 0010 §6).

Covers the dedicated unit's runner: the empty-token refusal guard, the app the
entrypoint assembles serving ``/healthz`` / ``/`` and enforcing bearer auth on a
gated route, and the managers being wired to a real :class:`EpisodeRewardsStore`
over the in-memory event table. Mirrors ``test_queue_endpoints``' style: build
the real app via the entrypoint's ``build_app`` helper and drive it with a
``TestClient`` — no socket is bound.
"""

from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient

from turing.config import TuringConfig
from turing.coordinator.episode_rewards import EpisodeRewardsStore, RewardSource
from turing.gateway.__main__ import (
    _check_token,
    build_app,
    build_assembly,
    build_resources,
)
from turing.gateway.queue_manager import QueueItem

_TOKEN = "t0k3n"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}


def _config(*, token: str = _TOKEN) -> TuringConfig:
    """A config that ignores any real ``.env`` (codebase fixture convention)."""
    return TuringConfig(  # type: ignore[call-arg]
        _env_file=None,
        gateway_token=token,
        gateway_bind="127.0.0.1",
        gateway_port=8765,
        node_name="pi-alpha",
    )


# ── (a) empty-token refusal ──────────────────────────────────────────────────


def test_check_token_refuses_empty_token() -> None:
    with pytest.raises(SystemExit) as exc:
        _check_token(_config(token=""))
    assert "TURING_GATEWAY_TOKEN" in str(exc.value)


def test_check_token_passes_with_token() -> None:
    # No raise == pass.
    _check_token(_config())


def test_build_assembly_refuses_empty_token_on_start() -> None:
    """``GatewayService.start`` is the second line of defence (same guard)."""

    async def _go() -> None:
        assembly = await build_assembly(_config(token=""))
        try:
            with pytest.raises(RuntimeError, match="gateway_token is empty"):
                await assembly.service.start()
        finally:
            await assembly.resources.ring_buffer.close()

    anyio.run(_go)


# ── (b) the assembled app serves /healthz, / and gates a bearer route ────────


def _client(config: TuringConfig) -> tuple[TestClient, object]:
    """Build the entrypoint's app and a ``TestClient`` over it.

    Returns the ring buffer too so the caller can close it — keeps the
    in-memory aiosqlite handle from leaking across tests.
    """

    async def _build() -> object:
        return await build_app(config)

    app = anyio.run(_build)
    return TestClient(app), app


def test_healthz_returns_200() -> None:
    client, _ = _client(_config())
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_root_returns_200_landing_or_spa() -> None:
    # ``/`` is public: unauthenticated callers get the friendly landing payload
    # (or the SPA bundle if one is built). Either way it's a 200, never a 401.
    client, _ = _client(_config())
    res = client.get("/")
    assert res.status_code == 200


def test_gated_route_401_without_token() -> None:
    client, _ = _client(_config())
    assert client.get("/api/queue").status_code == 401


def test_gated_route_200_with_token() -> None:
    client, _ = _client(_config())
    res = client.get("/api/queue", headers=_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["type"] == "queue.snapshot"
    # Projection starts empty (documented pre-IPC-split limitation).
    assert body["items"] == []


def test_gated_route_rejects_wrong_token() -> None:
    client, _ = _client(_config())
    res = client.get("/api/queue", headers={"Authorization": "Bearer wrong"})
    assert res.status_code == 401


# ── (c) managers wired to a real EpisodeRewardsStore over the event table ─────


def test_resources_share_one_rewards_store() -> None:
    """Queue + chat managers must write to the *same* store (shared instance)."""

    async def _go() -> EpisodeRewardsStore:
        resources = await build_resources(_config())
        try:
            # The chat manager delegates reward writes to a private QueueManager
            # over the same store; the public queue manager is a separate
            # projection but shares the identical rewards instance.
            assert resources.queue_manager._rewards is resources.rewards_store
            assert resources.chat_manager._rewards is resources.rewards_store
            return resources.rewards_store
        finally:
            await resources.ring_buffer.close()

    store = anyio.run(_go)
    assert isinstance(store, EpisodeRewardsStore)


def test_queue_accept_writes_reward_to_the_wired_store() -> None:
    """End-to-end: a curate-accept through the queue manager lands a +1.0 row in
    the entrypoint's wired :class:`EpisodeRewardsStore`."""

    async def _go() -> EpisodeRewardsStore:
        resources = await build_resources(_config())
        try:
            mgr = resources.queue_manager
            await mgr.add(QueueItem(id="q1", prompt="p", specialty="s"))
            await mgr.draft("q1", episode_id="ep-q1")
            await mgr.accept("q1")
            return resources.rewards_store
        finally:
            await resources.ring_buffer.close()

    store = anyio.run(_go)
    events = store.events_for("ep-q1")
    assert len(events) == 1
    assert events[0].source is RewardSource.MORNING_CURATION
    assert events[0].value == 1.0
