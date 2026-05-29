"""Gateway question-queue endpoints + WS frames (ADR 0010 Slice C).

Exercises the real FastAPI app: bearer-auth enforcement on the queue routes,
the approve / accept / reject / edit happy paths writing rewards through the
``QueueManager``, 404 on unknown items, and the WS contract — a
``queue.snapshot`` on connect followed by live ``queue.delta`` frames driven
through the shared telemetry-sink fan-out.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from turing.coordinator.episode_rewards import EpisodeRewardsStore, RewardSource
from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth
from turing.gateway.queue_manager import QueueItem, QueueManager
from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig
from turing.gateway.telemetry_sink import TelemetrySink

_HEADERS = {"Authorization": "Bearer t0k3n"}


def _const_clock(t: int = 7_000):
    return lambda: t


def _manager(rewards: EpisodeRewardsStore | None = None, broadcast=None) -> QueueManager:
    return QueueManager(
        rewards=rewards if rewards is not None else EpisodeRewardsStore(),
        broadcast=broadcast,
        now_ms=_const_clock(),
    )


def _client(manager: QueueManager, **kw) -> TestClient:
    app = create_app(
        auth=GatewayAuth(token="t0k3n"),  # type: ignore[call-arg]
        node_name="pi-alpha",
        queue_manager=manager,
        **kw,
    )
    return TestClient(app)


def _seed_drafted(manager: QueueManager, item_id: str = "q1", episode_id: str = "ep-q1") -> None:
    """Synchronously drive an item to DRAFTED for endpoint tests."""
    import anyio

    async def _go() -> None:
        await manager.add(QueueItem(id=item_id, prompt="p", specialty="s"))
        await manager.draft(item_id, episode_id=episode_id)

    anyio.run(_go)


# ── auth ─────────────────────────────────────────────────────────────────────


def test_queue_routes_require_bearer_auth() -> None:
    mgr = _manager()
    _seed_drafted(mgr)
    client = _client(mgr)
    for path in (
        "/api/queue/approve/q1",
        "/api/queue/accept/q1",
        "/api/queue/reject/q1",
    ):
        assert client.post(path).status_code == 401
    assert client.get("/api/queue").status_code == 401


def test_get_queue_returns_snapshot() -> None:
    mgr = _manager()
    _seed_drafted(mgr)
    client = _client(mgr)
    res = client.get("/api/queue", headers=_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["type"] == "queue.snapshot"
    assert [i["id"] for i in body["items"]] == ["q1"]
    assert body["items"][0]["status"] == "drafted"


# ── decisions write rewards through the manager ──────────────────────────────


def test_accept_endpoint_writes_plus_one_reward() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards)
    _seed_drafted(mgr)
    client = _client(mgr)
    res = client.post("/api/queue/accept/q1", headers=_HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "curated"
    assert res.json()["decision"] == "accept"
    events = rewards.events_for("ep-q1")
    assert len(events) == 1
    assert events[0].source is RewardSource.MORNING_CURATION
    assert events[0].value == 1.0


def test_reject_endpoint_writes_minus_one_reward() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards)
    _seed_drafted(mgr)
    client = _client(mgr)
    res = client.post("/api/queue/reject/q1", headers=_HEADERS)
    assert res.status_code == 200
    assert rewards.events_for("ep-q1")[0].value == -1.0


def test_edit_endpoint_with_corrected_answer_writes_fractional_reward() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards)
    _seed_drafted(mgr)
    client = _client(mgr)
    res = client.post(
        "/api/queue/edit/q1",
        headers=_HEADERS,
        json={"corrected_answer": "Rayleigh scattering."},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["decision"] == "edit"
    assert body["corrected_answer"] == "Rayleigh scattering."
    assert rewards.events_for("ep-q1")[0].value == 0.3


def test_approve_endpoint_writes_no_reward() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards)
    _seed_drafted(mgr, episode_id="ep-q1")
    client = _client(mgr)
    res = client.post("/api/queue/approve/q1", headers=_HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "approved"
    assert rewards.events_for("ep-q1") == []


def test_unknown_item_returns_404() -> None:
    mgr = _manager()
    client = _client(mgr)
    assert client.post("/api/queue/accept/ghost", headers=_HEADERS).status_code == 404


def test_queue_routes_503_when_manager_absent() -> None:
    app = create_app(auth=GatewayAuth(token="t0k3n"), node_name="pi-alpha")  # type: ignore[call-arg]
    client = TestClient(app)
    assert client.get("/api/queue", headers=_HEADERS).status_code == 503


# ── WebSocket: snapshot on connect, delta on mutation ────────────────────────


def test_ws_sends_queue_snapshot_on_connect() -> None:
    rewards = EpisodeRewardsStore()
    cfg = RingBufferConfig(path=Path(":memory:"), retention_seconds=10**9, max_bytes=10**9)
    buffer = RingBuffer(cfg)
    sink = TelemetrySink(buffer=buffer)
    mgr = _manager(rewards, broadcast=sink._broadcast)
    _seed_drafted(mgr)

    client = _client(mgr, telemetry_sink=sink, ring_buffer=buffer)
    with client.websocket_connect("/ws", headers=_HEADERS) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"
        snapshot = json.loads(ws.receive_text())
        assert snapshot["type"] == "queue.snapshot"
        assert [i["id"] for i in snapshot["items"]] == ["q1"]


def test_endpoint_broadcasts_delta_to_connected_ws() -> None:
    rewards = EpisodeRewardsStore()
    cfg = RingBufferConfig(path=Path(":memory:"), retention_seconds=10**9, max_bytes=10**9)
    buffer = RingBuffer(cfg)
    sink = TelemetrySink(buffer=buffer)
    mgr = _manager(rewards, broadcast=sink._broadcast)
    _seed_drafted(mgr)

    client = _client(mgr, telemetry_sink=sink, ring_buffer=buffer)
    with client.websocket_connect("/ws", headers=_HEADERS) as ws:
        json.loads(ws.receive_text())  # hello
        json.loads(ws.receive_text())  # snapshot
        # Curate via the HTTP endpoint; the resulting delta must reach the WS.
        res = client.post("/api/queue/accept/q1", headers=_HEADERS)
        assert res.status_code == 200
        delta = json.loads(ws.receive_text())
        assert delta["type"] == "queue.delta"
        assert delta["action"] == "accept"
        assert delta["item"]["id"] == "q1"
        assert delta["item"]["status"] == "curated"
