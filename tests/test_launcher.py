"""Tests for the operator launcher CLI (#47)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from turing.launcher import LauncherError, LauncherResult, run_launcher


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeHTTP:
    """Stand-in for httpx — records the requests, returns the queued reply."""

    def __init__(self, *responses: _FakeResponse) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> _FakeResponse:
        self.requests.append((url, headers))
        if not self._responses:
            raise RuntimeError("unexpected extra HTTP call")
        return self._responses.pop(0)


# ── happy path ────────────────────────────────────────────────────────


class TestHappyPath:
    def test_launcher_opens_handoff_url(self) -> None:
        http = _FakeHTTP(_FakeResponse(200, '{"status":"ok"}'))
        opened: list[str] = []

        result = run_launcher(
            host="100.64.0.1",
            port=8765,
            token="secret",
            http_get=http.get,
            open_browser=opened.append,
        )

        assert isinstance(result, LauncherResult)
        assert result.exit_code == 0
        assert opened == ["http://100.64.0.1:8765/token-handoff?token=secret"]

    def test_launcher_validates_token_via_healthz(self) -> None:
        http = _FakeHTTP(_FakeResponse(200))
        run_launcher(
            host="100.64.0.1",
            port=8765,
            token="secret",
            http_get=http.get,
            open_browser=lambda _: None,
        )
        url, headers = http.requests[0]
        # /healthz is the auth-bypass route — but the launcher hits it with
        # the bearer header anyway so a 401 from a stricter deployment still
        # surfaces here.
        assert url == "http://100.64.0.1:8765/healthz"
        assert headers.get("Authorization") == "Bearer secret"


# ── error paths ───────────────────────────────────────────────────────


class TestErrorPaths:
    def test_resolve_failure_exits_non_zero(self) -> None:
        with pytest.raises(LauncherError) as exc:
            run_launcher(
                host="",  # empty host
                port=8765,
                token="secret",
                http_get=lambda *_a, **_k: _FakeResponse(200),
                open_browser=lambda _: None,
            )
        assert "host" in str(exc.value).lower()
        assert exc.value.exit_code != 0

    def test_network_unreachable_exits_non_zero(self) -> None:
        def boom(*_a: Any, **_k: Any) -> _FakeResponse:
            raise ConnectionError("network unreachable")

        with pytest.raises(LauncherError) as exc:
            run_launcher(
                host="100.64.0.1",
                port=8765,
                token="secret",
                http_get=boom,
                open_browser=lambda _: None,
            )
        assert "network" in str(exc.value).lower() or "unreachable" in str(exc.value).lower()
        assert exc.value.exit_code != 0

    def test_401_exits_non_zero_with_clear_message(self) -> None:
        http = _FakeHTTP(_FakeResponse(401))
        with pytest.raises(LauncherError) as exc:
            run_launcher(
                host="100.64.0.1",
                port=8765,
                token="wrong",
                http_get=http.get,
                open_browser=lambda _: None,
            )
        assert "401" in str(exc.value) or "unauthorized" in str(exc.value).lower()
        assert exc.value.exit_code != 0


# ── main() smoke ──────────────────────────────────────────────────────


class TestMainSmoke:
    def test_main_returns_nonzero_on_auth_failure(self, capsys: pytest.CaptureFixture[str]) -> None:
        from turing.launcher import main

        http = _FakeHTTP(_FakeResponse(401))
        with (
            patch("turing.launcher._build_http_get", return_value=http.get),
            patch("turing.launcher._open_browser", lambda _u: None),
        ):
            exit_code = main(
                argv=[
                    "--host",
                    "100.64.0.1",
                    "--port",
                    "8765",
                    "--token",
                    "wrong",
                ]
            )
        assert exit_code != 0
        captured = capsys.readouterr()
        assert "401" in captured.err or "unauthorized" in captured.err.lower()

    def test_main_returns_zero_on_success(self, capsys: pytest.CaptureFixture[str]) -> None:
        from turing.launcher import main

        http = _FakeHTTP(_FakeResponse(200))
        with (
            patch("turing.launcher._build_http_get", return_value=http.get),
            patch("turing.launcher._open_browser", lambda _u: None),
        ):
            exit_code = main(
                argv=[
                    "--host",
                    "100.64.0.1",
                    "--port",
                    "8765",
                    "--token",
                    "secret",
                ]
            )
        assert exit_code == 0
