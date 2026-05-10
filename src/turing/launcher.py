"""Operator launcher CLI.

The user runs ``turing-ui`` on their laptop. This module:

1. Resolves pi-alpha (currently from ``--host`` / ``$TURING_GATEWAY_HOST``;
   mDNS lookup lands in a future slice).
2. Validates the bearer token by hitting ``/healthz`` with the
   ``Authorization`` header. A non-200 surfaces here even if the deployment
   left the route public, because the launcher's job is to fail loudly
   before the operator stares at a blank browser tab.
3. Opens the system default browser to ``/token-handoff?token=...`` —
   the gateway converts that to an http-only cookie and 303-redirects to
   ``/`` so the token never lives in the address bar after the first hop.

Every error path prints a clear message to stderr and exits non-zero.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass

HttpGetFn = Callable[..., object]
"""Signature: ``(url, *, headers, timeout) -> response`` where the response
exposes ``status_code`` and ``text`` attributes (``httpx.Response`` matches)."""

OpenBrowserFn = Callable[[str], None]


class LauncherError(RuntimeError):
    """Raised when the launcher cannot reach a usable gateway."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class LauncherResult:
    handoff_url: str
    exit_code: int


def run_launcher(
    *,
    host: str,
    port: int,
    token: str,
    http_get: HttpGetFn,
    open_browser: OpenBrowserFn,
    timeout_s: float = 5.0,
) -> LauncherResult:
    if not host:
        raise LauncherError("no gateway host configured — set --host or TURING_GATEWAY_HOST")
    if not token:
        raise LauncherError("no gateway token configured — set --token or TURING_GATEWAY_TOKEN")

    base = f"http://{host}:{port}"
    healthz = f"{base}/healthz"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        response = http_get(healthz, headers=headers, timeout=timeout_s)
    except Exception as exc:
        raise LauncherError(f"network unreachable: cannot reach {healthz}: {exc}") from exc

    status = getattr(response, "status_code", None)
    if status == 401:
        raise LauncherError(f"unauthorized (401) at {healthz} — token rejected")
    if status != 200:
        raise LauncherError(f"gateway returned {status} at {healthz}; expected 200")

    handoff = f"{base}/token-handoff?token={token}"
    open_browser(handoff)
    return LauncherResult(handoff_url=handoff, exit_code=0)


# ── CLI plumbing ─────────────────────────────────────────────────────


def _build_http_get() -> HttpGetFn:
    import httpx

    def _get(url: str, *, headers: dict, timeout: float) -> object:
        return httpx.get(url, headers=headers, timeout=timeout)

    return _get


def _open_browser(url: str) -> None:
    webbrowser.open(url)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="turing-ui",
        description=(
            "Open the operator UI hosted by pi-alpha's gateway. Resolves the "
            "host, validates the bearer token, and hands it off to the browser "
            "via a one-shot URL parameter."
        ),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("TURING_GATEWAY_HOST", ""),
        help="gateway host (default: $TURING_GATEWAY_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TURING_GATEWAY_PORT", "8765")),
        help="gateway TCP port (default: 8765 / $TURING_GATEWAY_PORT)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("TURING_GATEWAY_TOKEN", ""),
        help="bearer token (default: $TURING_GATEWAY_TOKEN)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        run_launcher(
            host=args.host,
            port=args.port,
            token=args.token,
            http_get=_build_http_get(),
            open_browser=_open_browser,
        )
    except LauncherError as exc:
        print(f"turing-ui: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
