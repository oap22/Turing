"""Tests for the gateway hosting the SPA scaffold (#43)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from turing.gateway.app import create_app
from turing.gateway.auth import COOKIE_NAME, GatewayAuth

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def assets_dir(tmp_path: Path) -> Path:
    root = tmp_path / "webui_dist"
    root.mkdir()
    (root / "index.html").write_text(
        "<!DOCTYPE html><html><body>turing</body></html>", encoding="utf-8"
    )
    sub = root / "assets"
    sub.mkdir()
    (sub / "main.js").write_text("console.log('hi')", encoding="utf-8")
    return root


@pytest.fixture
def client(assets_dir: Path) -> TestClient:
    app = create_app(
        auth=GatewayAuth(token="secret-token"),
        node_name="pi-alpha",
        spa_assets_dir=assets_dir,
    )
    return TestClient(app)


class TestSpaServing:
    def test_root_with_token_returns_index_html(self, client: TestClient) -> None:
        r = client.get("/", headers={"Authorization": "Bearer secret-token"})
        assert r.status_code == 200
        assert "turing" in r.text
        assert "text/html" in r.headers.get("content-type", "")

    def test_root_without_token_returns_401(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 401

    def test_static_asset_with_token_served(self, client: TestClient) -> None:
        r = client.get("/assets/main.js", headers={"Authorization": "Bearer secret-token"})
        assert r.status_code == 200
        assert "console.log" in r.text

    def test_static_asset_without_token_returns_401(self, client: TestClient) -> None:
        r = client.get("/assets/main.js")
        assert r.status_code == 401


class TestTokenHandoff:
    def test_handoff_with_correct_token_redirects_and_sets_cookie(self, client: TestClient) -> None:
        r = client.get("/token-handoff?token=secret-token", follow_redirects=False)
        # 303 redirect to "/"
        assert r.status_code in (302, 303)
        # The Set-Cookie header must include our cookie name + secret-token
        cookie = r.headers.get("set-cookie", "")
        assert COOKIE_NAME in cookie
        assert "secret-token" in cookie

    def test_handoff_with_wrong_token_returns_401(self, client: TestClient) -> None:
        r = client.get("/token-handoff?token=wrong")
        assert r.status_code == 401

    def test_handoff_with_missing_token_returns_401(self, client: TestClient) -> None:
        r = client.get("/token-handoff")
        assert r.status_code == 401

    def test_after_handoff_cookie_works_for_root(self, client: TestClient) -> None:
        # Step 1: visit /token-handoff?token=secret-token to get the cookie set
        client.get("/token-handoff?token=secret-token", follow_redirects=False)
        # Step 2: subsequent / request uses the cookie automatically
        r = client.get("/")
        assert r.status_code == 200


class TestNoAssetsConfigured:
    """When no spa_assets_dir is given, root returns a 404, not a crash."""

    def test_root_without_assets_returns_404(self) -> None:
        app = create_app(auth=GatewayAuth(token="secret-token"), node_name="pi-alpha")
        c = TestClient(app)
        r = c.get("/", headers={"Authorization": "Bearer secret-token"})
        assert r.status_code == 404
