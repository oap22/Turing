"""Tests for the NetworkTool.

Every external boundary is mocked — no real packets, sockets, DNS, or HTTP
leave the test process. ``ping`` shells out, so ``create_subprocess_shell`` is
patched; ``http_request`` uses ``httpx.AsyncClient``; ``dns_lookup`` uses
``socket.getaddrinfo``; ``port_check`` uses ``asyncio.open_connection``.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from turing.tools.base import RiskLevel
from turing.tools.network import NetworkTool


@pytest.fixture()
def network_tool() -> NetworkTool:
    return NetworkTool()


# ---------------------------------------------------------------------------
# Contract + dispatch
# ---------------------------------------------------------------------------


class TestContract:
    def test_identity_and_risk(self, network_tool: NetworkTool):
        assert network_tool.name == "network"
        assert network_tool.risk_level is RiskLevel.MEDIUM
        assert network_tool.requires_confirmation is False

    def test_schema_actions(self, network_tool: NetworkTool):
        enum = network_tool.parameters["properties"]["action"]["enum"]
        assert set(enum) == {"ping", "http_request", "dns_lookup", "port_check"}

    async def test_missing_action(self, network_tool: NetworkTool):
        result = await network_tool.execute()
        assert result.success is False
        assert "No action specified" in result.error

    async def test_unknown_action(self, network_tool: NetworkTool):
        result = await network_tool.execute(action="telnet")
        assert result.success is False
        assert "Unknown action 'telnet'" in result.error

    async def test_handler_exception_is_caught(self, network_tool: NetworkTool):
        with patch("turing.tools.network.socket.getaddrinfo", side_effect=RuntimeError("kaboom")):
            result = await network_tool.execute(action="dns_lookup", host="example.com")
        assert result.success is False
        assert "kaboom" in result.error


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------


def _patch_ping_subprocess(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return patch(
        "turing.tools.network.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    )


class TestPing:
    async def test_missing_host(self, network_tool: NetworkTool):
        result = await network_tool.execute(action="ping")
        assert result.success is False
        assert "No host specified" in result.error

    async def test_successful_ping(self, network_tool: NetworkTool):
        with _patch_ping_subprocess(0, stdout=b"64 bytes from 1.1.1.1") as p:
            result = await network_tool.execute(action="ping", host="1.1.1.1")
        assert result.success is True
        assert "64 bytes" in result.output
        # No shell: the host is a separate argv token, never a command string.
        argv = p.await_args.args
        assert argv[0] == "ping"
        assert argv[1:3] == ("-c", "4")
        assert argv[-1] == "1.1.1.1"

    async def test_count_is_clamped_to_ten(self, network_tool: NetworkTool):
        with _patch_ping_subprocess(0, stdout=b"ok") as p:
            await network_tool.execute(action="ping", host="1.1.1.1", count=999)
        argv = p.await_args.args
        assert argv[1:3] == ("-c", "10")

    async def test_failed_ping_returns_stderr(self, network_tool: NetworkTool):
        with _patch_ping_subprocess(1, stderr=b"unknown host"):
            result = await network_tool.execute(action="ping", host="nope.invalid")
        assert result.success is False
        assert "unknown host" in result.error

    async def test_timeout(self, network_tool: NetworkTool):
        # communicate() raises so wait_for propagates TimeoutError without
        # leaving a dangling, never-awaited coroutine.
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(side_effect=TimeoutError)
        with patch(
            "turing.tools.network.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            result = await network_tool.execute(action="ping", host="1.1.1.1")
        assert result.success is False
        assert "timed out" in result.error

    @pytest.mark.parametrize(
        "host",
        [
            "8.8.8.8; rm -rf /",
            "$(touch /tmp/pwned)",
            "`id`",
            "host name with spaces",
            "-flag",
            "a&b",
            "a|b",
        ],
    )
    async def test_rejects_injection_payloads(self, network_tool: NetworkTool, host: str):
        """Hosts containing shell metacharacters / whitespace are rejected.

        Regression guard for the command-injection fix (#312): the value never
        reaches a subprocess, so no shell can interpret it.
        """
        with _patch_ping_subprocess(0) as p:
            result = await network_tool.execute(action="ping", host=host)
        assert result.success is False
        assert "Invalid host" in result.error
        p.assert_not_awaited()

    async def test_accepts_legitimate_hosts(self, network_tool: NetworkTool):
        for host in ("example.com", "1.1.1.1", "my-host.local", "2606:4700:4700::1111"):
            with _patch_ping_subprocess(0, stdout=b"ok"):
                result = await network_tool.execute(action="ping", host=host)
            assert result.success is True


# ---------------------------------------------------------------------------
# http_request
# ---------------------------------------------------------------------------


def _fake_httpx_client(response: MagicMock) -> MagicMock:
    """Return a stand-in for ``httpx.AsyncClient`` usable as an async CM."""
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return factory, client


def _fake_response(*, status_code: int = 200, text: str = "hello", reason: str = "OK") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason_phrase = reason
    resp.text = text
    resp.content = text.encode()
    resp.headers = {"content-type": "text/plain"}
    resp.is_redirect = False
    return resp


def _public_addrinfo(*_args, **_kwargs):
    """Stand-in for ``socket.getaddrinfo`` that resolves to a public address.

    The SSRF guard resolves every URL/host before connecting; tests mock the
    HTTP/socket layer, so the resolver is mocked too to return a routable IP.
    """
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


@pytest.fixture(autouse=True)
def _patch_public_dns():
    with patch("turing.tools.network.socket.getaddrinfo", side_effect=_public_addrinfo):
        yield


class TestHttpRequest:
    async def test_missing_url(self, network_tool: NetworkTool):
        result = await network_tool.execute(action="http_request")
        assert result.success is False
        assert "No URL specified" in result.error

    async def test_unsupported_method(self, network_tool: NetworkTool):
        result = await network_tool.execute(action="http_request", url="http://x", method="DELETE")
        assert result.success is False
        assert "Unsupported HTTP method" in result.error

    async def test_successful_get(self, network_tool: NetworkTool):
        factory, client = _fake_httpx_client(_fake_response(text="payload"))
        with patch("turing.tools.network.httpx.AsyncClient", factory):
            result = await network_tool.execute(action="http_request", url="http://example.com")
        assert result.success is True
        assert "Status: 200 OK" in result.output
        assert "payload" in result.output
        client.get.assert_awaited_once_with("http://example.com")

    async def test_post_passes_body(self, network_tool: NetworkTool):
        factory, client = _fake_httpx_client(_fake_response(text="created", status_code=201))
        with patch("turing.tools.network.httpx.AsyncClient", factory):
            result = await network_tool.execute(
                action="http_request", url="http://x", method="POST", body="data"
            )
        assert result.success is True
        client.post.assert_awaited_once_with("http://x", content="data")

    async def test_4xx_is_failure(self, network_tool: NetworkTool):
        factory, _ = _fake_httpx_client(
            _fake_response(status_code=404, reason="Not Found", text="missing")
        )
        with patch("turing.tools.network.httpx.AsyncClient", factory):
            result = await network_tool.execute(action="http_request", url="http://x")
        assert result.success is False
        assert "HTTP 404" in result.error

    async def test_large_body_is_truncated(self, network_tool: NetworkTool):
        big = "x" * 5000
        factory, _ = _fake_httpx_client(_fake_response(text=big))
        with patch("turing.tools.network.httpx.AsyncClient", factory):
            result = await network_tool.execute(action="http_request", url="http://x")
        assert result.truncated is True
        assert "(response truncated)" in result.output

    async def test_timeout(self, network_tool: NetworkTool):
        factory = MagicMock(side_effect=httpx.TimeoutException("slow"))
        with patch("turing.tools.network.httpx.AsyncClient", factory):
            result = await network_tool.execute(action="http_request", url="http://x")
        assert result.success is False
        assert "timed out" in result.error

    async def test_request_error(self, network_tool: NetworkTool):
        factory = MagicMock(side_effect=httpx.ConnectError("refused"))
        with patch("turing.tools.network.httpx.AsyncClient", factory):
            result = await network_tool.execute(action="http_request", url="http://x")
        assert result.success is False
        assert "HTTP request failed" in result.error


# ---------------------------------------------------------------------------
# SSRF egress guard (issue: http_request / port_check could reach internal hosts)
# ---------------------------------------------------------------------------


def _addrinfo_for(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


class TestSsrfGuard:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # loopback
            "169.254.169.254",  # cloud metadata / link-local
            "10.0.0.5",  # RFC-1918 private
            "192.168.1.10",  # RFC-1918 private
            "172.16.0.1",  # RFC-1918 private
            "0.0.0.0",  # unspecified
            "::1",  # IPv6 loopback
        ],
    )
    async def test_http_request_blocks_internal_addresses(self, network_tool: NetworkTool, ip: str):
        factory, client = _fake_httpx_client(_fake_response())
        with (
            patch(
                "turing.tools.network.socket.getaddrinfo",
                side_effect=lambda *a, **k: _addrinfo_for(ip),
            ),
            patch("turing.tools.network.httpx.AsyncClient", factory),
        ):
            result = await network_tool.execute(
                action="http_request", url="http://internal.example/"
            )
        assert result.success is False
        assert "SSRF" in result.error
        client.get.assert_not_awaited()

    async def test_http_request_rejects_non_http_scheme(self, network_tool: NetworkTool):
        result = await network_tool.execute(action="http_request", url="file:///etc/passwd")
        assert result.success is False
        assert "scheme" in result.error.lower()

    async def test_http_request_blocks_redirect_to_internal(self, network_tool: NetworkTool):
        """A public URL that redirects to an internal address is re-validated and blocked."""
        redirect = _fake_response(status_code=302)
        redirect.is_redirect = True
        redirect.headers = {"location": "http://169.254.169.254/latest/meta-data/"}
        factory, _client = _fake_httpx_client(redirect)

        def _resolver(host, *_a, **_k):
            return _addrinfo_for(
                "169.254.169.254" if host == "169.254.169.254" else "93.184.216.34"
            )

        with (
            patch("turing.tools.network.socket.getaddrinfo", side_effect=_resolver),
            patch("turing.tools.network.httpx.AsyncClient", factory),
        ):
            result = await network_tool.execute(action="http_request", url="http://public.example/")
        assert result.success is False
        assert "SSRF" in result.error

    async def test_port_check_blocks_internal(self, network_tool: NetworkTool):
        opener = AsyncMock()
        with (
            patch(
                "turing.tools.network.socket.getaddrinfo",
                side_effect=lambda *a, **k: _addrinfo_for("127.0.0.1"),
            ),
            patch("turing.tools.network.asyncio.open_connection", opener),
        ):
            result = await network_tool.execute(action="port_check", host="localhost", port=22)
        assert result.success is False
        assert "SSRF" in result.error
        opener.assert_not_awaited()


# ---------------------------------------------------------------------------
# dns_lookup
# ---------------------------------------------------------------------------


class TestDnsLookup:
    async def test_missing_host(self, network_tool: NetworkTool):
        result = await network_tool.execute(action="dns_lookup")
        assert result.success is False
        assert "No host specified" in result.error

    async def test_resolves_and_dedupes(self, network_tool: NetworkTool):
        addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0)),  # dup
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
        ]
        with patch("turing.tools.network.socket.getaddrinfo", return_value=addrinfo):
            result = await network_tool.execute(action="dns_lookup", host="example.com")
        assert result.success is True
        assert result.output.count("1.2.3.4") == 1
        assert "IPv4: 1.2.3.4" in result.output
        assert "IPv6: ::1" in result.output

    async def test_empty_results(self, network_tool: NetworkTool):
        with patch("turing.tools.network.socket.getaddrinfo", return_value=[]):
            result = await network_tool.execute(action="dns_lookup", host="void")
        assert result.success is False
        assert "No DNS records found" in result.error

    async def test_gaierror(self, network_tool: NetworkTool):
        with patch(
            "turing.tools.network.socket.getaddrinfo",
            side_effect=socket.gaierror("name resolution failed"),
        ):
            result = await network_tool.execute(action="dns_lookup", host="nope.invalid")
        assert result.success is False
        assert "DNS lookup failed" in result.error


# ---------------------------------------------------------------------------
# port_check
# ---------------------------------------------------------------------------


class TestPortCheck:
    async def test_missing_host(self, network_tool: NetworkTool):
        result = await network_tool.execute(action="port_check", port=80)
        assert result.success is False
        assert "No host specified" in result.error

    async def test_missing_port(self, network_tool: NetworkTool):
        result = await network_tool.execute(action="port_check", host="x")
        assert result.success is False
        assert "No port specified" in result.error

    async def test_open_port(self, network_tool: NetworkTool):
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        opener = AsyncMock(return_value=(MagicMock(), writer))
        with patch("turing.tools.network.asyncio.open_connection", opener):
            result = await network_tool.execute(action="port_check", host="x", port=22)
        assert result.success is True
        assert "is OPEN" in result.output
        writer.close.assert_called_once()

    async def test_timeout_means_filtered(self, network_tool: NetworkTool):
        opener = AsyncMock(side_effect=TimeoutError)
        with patch("turing.tools.network.asyncio.open_connection", opener):
            result = await network_tool.execute(action="port_check", host="x", port=22)
        assert result.success is False
        assert "CLOSED or FILTERED" in result.output

    async def test_connection_refused(self, network_tool: NetworkTool):
        opener = AsyncMock(side_effect=ConnectionRefusedError)
        with patch("turing.tools.network.asyncio.open_connection", opener):
            result = await network_tool.execute(action="port_check", host="x", port=22)
        assert result.success is False
        assert "connection refused" in result.output

    async def test_oserror(self, network_tool: NetworkTool):
        opener = AsyncMock(side_effect=OSError("network unreachable"))
        with patch("turing.tools.network.asyncio.open_connection", opener):
            result = await network_tool.execute(action="port_check", host="x", port=22)
        assert result.success is False
        assert "Port check failed" in result.error
