"""Tests for the friendly landing response on bare ``GET /``.

A new operator opening the gateway URL in a browser previously hit a raw 401
with no hint of what to do next. The gateway now exposes ``/`` as a public
landing page that names the service and points to ``/token-handoff`` so the
operator can supply a token and proceed to the SPA.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth

if TYPE_CHECKING:
    from pathlib import Path


class TestBareRootNoSpa:
    """Without an SPA bundle configured, ``/`` must still be friendly."""

    @pytest.fixture
    def client(self) -> TestClient:
        app = create_app(auth=GatewayAuth(token="secret-token"), node_name="pi-alpha")
        return TestClient(app)

    def test_root_without_auth_returns_200(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200

    def test_root_response_names_the_service(self, client: TestClient) -> None:
        r = client.get("/")
        body = r.json()
        assert body.get("service") == "turing-gateway"

    def test_root_response_points_at_token_handoff(self, client: TestClient) -> None:
        r = client.get("/")
        body = r.json()
        # Operator needs a real next step — the token-handoff route is where
        # auth actually happens, so the landing response must mention it.
        assert "token-handoff" in body.get("login", "")


class TestBareRootWithSpa:
    """When the SPA is configured, authed users still get the SPA at ``/``."""

    @pytest.fixture
    def assets_dir(self, tmp_path: Path) -> Path:
        root = tmp_path / "webui_dist"
        root.mkdir()
        (root / "index.html").write_text(
            "<!DOCTYPE html><html><body>turing-spa-marker</body></html>",
            encoding="utf-8",
        )
        return root

    @pytest.fixture
    def client(self, assets_dir: Path) -> TestClient:
        app = create_app(
            auth=GatewayAuth(token="secret-token"),
            node_name="pi-alpha",
            spa_assets_dir=assets_dir,
        )
        return TestClient(app)

    def test_root_with_token_still_serves_spa(self, client: TestClient) -> None:
        r = client.get("/", headers={"Authorization": "Bearer secret-token"})
        assert r.status_code == 200
        assert "turing-spa-marker" in r.text

    def test_root_without_token_returns_friendly_landing(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200
        # Friendly JSON, not the SPA HTML — unauthed visitors get the pointer.
        assert r.json().get("service") == "turing-gateway"
