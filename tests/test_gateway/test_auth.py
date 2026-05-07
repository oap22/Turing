"""Bearer-token auth middleware tests for the pi-alpha gateway."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth


@pytest.fixture
def client() -> TestClient:
    app = create_app(auth=GatewayAuth(token="secret-token"), node_name="pi-alpha")
    return TestClient(app)


class TestBearerHeader:
    def test_missing_auth_returns_401(self, client: TestClient) -> None:
        r = client.get("/static-stub")
        assert r.status_code == 401

    def test_wrong_token_returns_401(self, client: TestClient) -> None:
        r = client.get("/static-stub", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

    def test_correct_token_returns_200(self, client: TestClient) -> None:
        r = client.get(
            "/static-stub", headers={"Authorization": "Bearer secret-token"}
        )
        # The /static-stub route exists in the app for testing; it returns 200
        # only when auth passes. Specific response body is unimportant.
        assert r.status_code == 200


class TestCookieFallback:
    def test_cookie_accepted_when_header_missing(self, client: TestClient) -> None:
        client.cookies.set("turing_gateway_token", "secret-token")
        r = client.get("/static-stub")
        assert r.status_code == 200

    def test_wrong_cookie_returns_401(self, client: TestClient) -> None:
        client.cookies.set("turing_gateway_token", "wrong")
        r = client.get("/static-stub")
        assert r.status_code == 401


class TestHealthcheckBypass:
    """The healthcheck route is exempt — the load balancer must reach it."""

    def test_healthcheck_does_not_require_auth(self, client: TestClient) -> None:
        r = client.get("/healthz")
        assert r.status_code == 200
