"""WebSocket handshake contract for the pi-alpha gateway."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth


@pytest.fixture
def client() -> TestClient:
    app = create_app(auth=GatewayAuth(token="secret-token"), node_name="pi-alpha")
    return TestClient(app)


class TestWebSocketAuth:
    def test_ws_without_token_is_rejected(self, client: TestClient) -> None:
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws") as ws:
            ws.receive_text()

    def test_ws_with_wrong_token_is_rejected(self, client: TestClient) -> None:
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws", headers={"Authorization": "Bearer wrong"}) as ws,
        ):
            ws.receive_text()


class TestWebSocketHelloFrame:
    def test_handshake_succeeds_with_correct_token(self, client: TestClient) -> None:
        with client.websocket_connect(
            "/ws", headers={"Authorization": "Bearer secret-token"}
        ) as ws:
            frame = json.loads(ws.receive_text())
            assert frame["type"] == "hello"

    def test_hello_frame_includes_node_name(self, client: TestClient) -> None:
        with client.websocket_connect(
            "/ws", headers={"Authorization": "Bearer secret-token"}
        ) as ws:
            frame = json.loads(ws.receive_text())
            assert frame["node_name"] == "pi-alpha"

    def test_hello_frame_includes_uptime_seconds(self, client: TestClient) -> None:
        with client.websocket_connect(
            "/ws", headers={"Authorization": "Bearer secret-token"}
        ) as ws:
            frame = json.loads(ws.receive_text())
            assert "uptime_s" in frame
            assert isinstance(frame["uptime_s"], (int, float))
            assert frame["uptime_s"] >= 0

    def test_ws_accepts_cookie_token(self, client: TestClient) -> None:
        client.cookies.set("turing_gateway_token", "secret-token")
        with client.websocket_connect("/ws") as ws:
            frame = json.loads(ws.receive_text())
            assert frame["type"] == "hello"
