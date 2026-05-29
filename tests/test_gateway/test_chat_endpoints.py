"""Gateway chat-pane endpoints + WS frames (ADR 0010 Slice E).

Exercises the real FastAPI app: bearer-auth enforcement on the chat routes,
the submit + per-subtask accept / reject / edit happy paths writing rewards
through the REUSED Slice C emitter, 404 on unknown sessions/subtasks, 503 when
the manager is absent, and the WS contract — a ``chat.snapshot`` on connect
followed by live ``chat.delta`` frames driven through the shared telemetry-sink
fan-out. Mirrors ``test_queue_endpoints.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from turing.coordinator.episode_rewards import EpisodeRewardsStore, RewardSource
from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth
from turing.gateway.chat_manager import ChatManager
from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig
from turing.gateway.telemetry_sink import TelemetrySink

_HEADERS = {"Authorization": "Bearer t0k3n"}


def _const_clock(t: int = 7_000):
    return lambda: t


def _manager(rewards: EpisodeRewardsStore | None = None, broadcast=None) -> ChatManager:
    return ChatManager(
        rewards=rewards if rewards is not None else EpisodeRewardsStore(),
        broadcast=broadcast,
        now_ms=_const_clock(),
    )


def _client(manager: ChatManager, **kw) -> TestClient:
    app = create_app(
        auth=GatewayAuth(token="t0k3n"),  # type: ignore[call-arg]
        node_name="pi-alpha",
        chat_manager=manager,
        **kw,
    )
    return TestClient(app)


def _seed_completed(
    manager: ChatManager,
    *,
    session_id: str = "s1",
    subtask_id: str = "st1",
    episode_id: str = "ep1",
) -> None:
    """Synchronously drive a subtask to COMPLETED for endpoint tests."""
    import anyio

    async def _go() -> None:
        await manager.submit(session_id=session_id, prompt="p")
        await manager.plan_subtask(
            session_id, subtask_id=subtask_id, specialty="research", episode_id=episode_id
        )
        await manager.stream(session_id, subtask_id, chunk="answer", done=True)

    anyio.run(_go)


# ── auth ─────────────────────────────────────────────────────────────────────


def test_chat_routes_require_bearer_auth() -> None:
    mgr = _manager()
    _seed_completed(mgr)
    client = _client(mgr)
    for path in (
        "/api/chat/s1/st1/accept",
        "/api/chat/s1/st1/reject",
    ):
        assert client.post(path).status_code == 401
    assert client.post("/api/chat/submit", json={"prompt": "x"}).status_code == 401
    assert client.get("/api/chat").status_code == 401


# ── submit ─────────────────────────────────────────────────────────────────────


def test_submit_creates_session() -> None:
    mgr = _manager()
    client = _client(mgr)
    res = client.post(
        "/api/chat/submit",
        headers=_HEADERS,
        json={"prompt": "why is the sky blue?", "session_id": "s1"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "s1"
    assert body["prompt"] == "why is the sky blue?"
    assert body["subtasks"] == []


def test_submit_without_session_id_mints_one() -> None:
    mgr = _manager()
    client = _client(mgr)
    res = client.post("/api/chat/submit", headers=_HEADERS, json={"prompt": "p"})
    assert res.status_code == 200
    assert res.json()["id"].startswith("chat-")


def test_get_chat_returns_snapshot() -> None:
    mgr = _manager()
    _seed_completed(mgr)
    client = _client(mgr)
    res = client.get("/api/chat", headers=_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["type"] == "chat.snapshot"
    assert [s["id"] for s in body["sessions"]] == ["s1"]
    assert body["sessions"][0]["subtasks"][0]["status"] == "completed"


# ── per-subtask thumbs write rewards through the reused emitter ────────────────


def test_accept_endpoint_writes_plus_one_reward() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards)
    _seed_completed(mgr)
    client = _client(mgr)
    res = client.post("/api/chat/s1/st1/accept", headers=_HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "curated"
    assert res.json()["decision"] == "accept"
    events = rewards.events_for("ep1")
    assert len(events) == 1
    assert events[0].source is RewardSource.MORNING_CURATION
    assert events[0].value == 1.0


def test_reject_endpoint_writes_minus_one_reward() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards)
    _seed_completed(mgr)
    client = _client(mgr)
    res = client.post("/api/chat/s1/st1/reject", headers=_HEADERS)
    assert res.status_code == 200
    assert rewards.events_for("ep1")[0].value == -1.0


def test_edit_endpoint_with_corrected_answer_writes_fractional_reward() -> None:
    rewards = EpisodeRewardsStore()
    mgr = _manager(rewards)
    _seed_completed(mgr)
    client = _client(mgr)
    res = client.post(
        "/api/chat/s1/st1/edit",
        headers=_HEADERS,
        json={"corrected_answer": "Rayleigh scattering."},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["decision"] == "edit"
    assert body["corrected_answer"] == "Rayleigh scattering."
    assert rewards.events_for("ep1")[0].value == 0.3


def test_unknown_session_returns_404() -> None:
    mgr = _manager()
    client = _client(mgr)
    assert client.post("/api/chat/ghost/st1/accept", headers=_HEADERS).status_code == 404


def test_unknown_subtask_returns_404() -> None:
    mgr = _manager()
    _seed_completed(mgr)
    client = _client(mgr)
    assert client.post("/api/chat/s1/ghost/accept", headers=_HEADERS).status_code == 404


def test_chat_routes_503_when_manager_absent() -> None:
    app = create_app(auth=GatewayAuth(token="t0k3n"), node_name="pi-alpha")  # type: ignore[call-arg]
    client = TestClient(app)
    assert client.get("/api/chat", headers=_HEADERS).status_code == 503
    assert (
        client.post("/api/chat/submit", headers=_HEADERS, json={"prompt": "p"}).status_code == 503
    )


# ── WebSocket: snapshot on connect, delta on mutation ────────────────────────


def test_ws_sends_chat_snapshot_on_connect() -> None:
    rewards = EpisodeRewardsStore()
    cfg = RingBufferConfig(path=Path(":memory:"), retention_seconds=10**9, max_bytes=10**9)
    buffer = RingBuffer(cfg)
    sink = TelemetrySink(buffer=buffer)
    mgr = _manager(rewards, broadcast=sink._broadcast)
    _seed_completed(mgr)

    client = _client(mgr, telemetry_sink=sink, ring_buffer=buffer)
    with client.websocket_connect("/ws", headers=_HEADERS) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"
        snapshot = json.loads(ws.receive_text())
        assert snapshot["type"] == "chat.snapshot"
        assert [s["id"] for s in snapshot["sessions"]] == ["s1"]


def test_endpoint_broadcasts_chat_delta_to_connected_ws() -> None:
    rewards = EpisodeRewardsStore()
    cfg = RingBufferConfig(path=Path(":memory:"), retention_seconds=10**9, max_bytes=10**9)
    buffer = RingBuffer(cfg)
    sink = TelemetrySink(buffer=buffer)
    mgr = _manager(rewards, broadcast=sink._broadcast)
    _seed_completed(mgr)

    client = _client(mgr, telemetry_sink=sink, ring_buffer=buffer)
    with client.websocket_connect("/ws", headers=_HEADERS) as ws:
        json.loads(ws.receive_text())  # hello
        json.loads(ws.receive_text())  # chat snapshot
        # Thumb via the HTTP endpoint; the resulting delta must reach the WS.
        res = client.post("/api/chat/s1/st1/accept", headers=_HEADERS)
        assert res.status_code == 200
        delta = json.loads(ws.receive_text())
        assert delta["type"] == "chat.delta"
        assert delta["action"] == "accept"
        assert delta["session_id"] == "s1"
        assert delta["subtask"]["id"] == "st1"
        assert delta["subtask"]["status"] == "curated"
