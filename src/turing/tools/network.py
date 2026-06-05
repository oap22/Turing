"""Network operations tool for connectivity diagnostics and HTTP requests."""

from __future__ import annotations

import asyncio
import re
import socket
from typing import Any

import httpx
import structlog

from turing.tools.base import RiskLevel, Tool, ToolResult

logger = structlog.get_logger("turing.tools.network")

# Default timeout for network operations (seconds).
DEFAULT_TIMEOUT = 10

# A hostname or IP literal: alphanumerics plus ``. - _ :`` (the colon covers
# IPv6 literals). The first character must be alphanumeric so a value can never
# be interpreted as a ``ping`` flag, and the set excludes whitespace and every
# shell metacharacter. Paired with create_subprocess_exec (no shell) this makes
# command injection impossible.
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]*$")


class NetworkTool(Tool):
    """Perform network operations: ping, HTTP requests, DNS lookup, and port checks."""

    @property
    def name(self) -> str:
        return "network"

    @property
    def description(self) -> str:
        return (
            "Perform network operations. Supported actions: "
            "ping, http_request, dns_lookup, port_check"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["ping", "http_request", "dns_lookup", "port_check"],
                    "description": "The network action to perform",
                },
                "host": {
                    "type": "string",
                    "description": "Hostname or IP address",
                },
                "url": {
                    "type": "string",
                    "description": "URL for http_request action",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST"],
                    "description": "HTTP method (default: GET)",
                },
                "body": {
                    "type": "string",
                    "description": "Request body for POST requests",
                },
                "port": {
                    "type": "integer",
                    "description": "Port number for port_check action",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of ping packets (default: 4, max: 10)",
                },
            },
            "required": ["action"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM

    async def execute(self, **kwargs: Any) -> ToolResult:
        action: str = kwargs.get("action", "")
        if not action:
            return ToolResult(success=False, output="", error="No action specified")

        dispatch = {
            "ping": self._ping,
            "http_request": self._http_request,
            "dns_lookup": self._dns_lookup,
            "port_check": self._port_check,
        }

        handler = dispatch.get(action)
        if handler is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown action '{action}'. Valid: {', '.join(dispatch)}",
            )

        try:
            return await handler(**kwargs)
        except Exception as exc:
            logger.error("network_error", action=action, error=str(exc))
            return ToolResult(success=False, output="", error=str(exc))

    async def _ping(self, **kwargs: Any) -> ToolResult:
        """Ping a host using the system ping command."""
        host = kwargs.get("host", "")
        if not host:
            return ToolResult(success=False, output="", error="No host specified")

        count = min(kwargs.get("count", 4), 10)

        # Reject anything that is not a plausible host/IP. With the no-shell
        # exec below, a value like ``8.8.8.8; rm -rf /`` can never reach a shell
        # — and it is rejected here anyway.
        safe_host = host.strip()
        if not _HOST_RE.match(safe_host):
            return ToolResult(success=False, output="", error=f"Invalid host '{host}'")

        try:
            process = await asyncio.create_subprocess_exec(
                "ping",
                "-c",
                str(count),
                "-W",
                str(DEFAULT_TIMEOUT),
                safe_host,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=DEFAULT_TIMEOUT + count * 2,
            )

            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            success = process.returncode == 0
            output = stdout_text if stdout_text else stderr_text
            return ToolResult(
                success=success,
                output=output,
                error=stderr_text if not success else "",
            )
        except TimeoutError:
            return ToolResult(success=False, output="", error=f"Ping to {safe_host} timed out")

    async def _http_request(self, **kwargs: Any) -> ToolResult:
        """Make an HTTP GET or POST request."""
        url = kwargs.get("url", "")
        if not url:
            return ToolResult(success=False, output="", error="No URL specified")

        method = kwargs.get("method", "GET").upper()
        if method not in ("GET", "POST"):
            return ToolResult(
                success=False,
                output="",
                error=f"Unsupported HTTP method '{method}'. Only GET and POST are allowed.",
            )

        body = kwargs.get("body")

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
                if method == "GET":
                    response = await client.get(url)
                else:
                    response = await client.post(url, content=body)

            # Truncate response body if very large.
            response_text = response.text
            truncated = False
            if len(response_text) > 4000:
                response_text = response_text[:4000] + "\n... (response truncated)"
                truncated = True

            lines = [
                f"HTTP {method} {url}",
                f"Status: {response.status_code} {response.reason_phrase}",
                f"Content-Type: {response.headers.get('content-type', 'unknown')}",
                f"Content-Length: {len(response.content)} bytes",
                "",
                response_text,
            ]

            return ToolResult(
                success=200 <= response.status_code < 400,
                output="\n".join(lines),
                error="" if 200 <= response.status_code < 400 else f"HTTP {response.status_code}",
                truncated=truncated,
            )
        except httpx.TimeoutException:
            return ToolResult(success=False, output="", error=f"HTTP request to {url} timed out")
        except httpx.RequestError as exc:
            return ToolResult(success=False, output="", error=f"HTTP request failed: {exc}")

    async def _dns_lookup(self, **kwargs: Any) -> ToolResult:
        """Perform a DNS lookup for a hostname."""
        host = kwargs.get("host", "")
        if not host:
            return ToolResult(success=False, output="", error="No host specified")

        loop = asyncio.get_running_loop()
        try:
            results = await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM),
            )

            lines = [f"DNS lookup results for '{host}':"]
            seen: set[str] = set()
            for family, _type, _proto, _canonname, sockaddr in results:
                addr = str(sockaddr[0])
                if addr in seen:
                    continue
                seen.add(addr)
                family_name = "IPv4" if family == socket.AF_INET else "IPv6"
                lines.append(f"  {family_name}: {addr}")

            if not seen:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"No DNS records found for '{host}'",
                )

            return ToolResult(success=True, output="\n".join(lines))
        except socket.gaierror as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"DNS lookup failed for '{host}': {exc}",
            )

    async def _port_check(self, **kwargs: Any) -> ToolResult:
        """Check if a specific port is open on a host."""
        host = kwargs.get("host", "")
        port = kwargs.get("port")

        if not host:
            return ToolResult(success=False, output="", error="No host specified")
        if port is None:
            return ToolResult(success=False, output="", error="No port specified")

        port = int(port)
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=DEFAULT_TIMEOUT,
            )
            writer.close()
            await writer.wait_closed()
            return ToolResult(
                success=True,
                output=f"Port {port} on {host} is OPEN",
            )
        except TimeoutError:
            return ToolResult(
                success=False,
                output=f"Port {port} on {host} is CLOSED or FILTERED (timeout)",
                error="Connection timed out",
            )
        except ConnectionRefusedError:
            return ToolResult(
                success=False,
                output=f"Port {port} on {host} is CLOSED (connection refused)",
                error="Connection refused",
            )
        except OSError as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Port check failed: {exc}",
            )
