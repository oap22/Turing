"""FastAPI app factory for the pi-alpha gateway.

Hosts the bearer-gated WebSocket (slice 3), serves the SPA bundle from a
configurable assets directory (slice 5), and exposes a ``/token-handoff``
helper that lets the CLI launcher convert a one-shot ``?token=...`` URL into
a cookie-based session before redirecting to the SPA. ``/healthz`` and
``/token-handoff`` are the only auth-bypass routes — every other route, the
WS upgrade included, requires a valid token.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI, Request, Response, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
)
from starlette.staticfiles import StaticFiles

from turing.gateway.auth import COOKIE_NAME, GatewayAuth

PUBLIC_PATHS = frozenset({"/healthz", "/token-handoff"})


class _BearerMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, auth: GatewayAuth) -> None:
        super().__init__(app)
        self._auth = auth

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        if self._auth.check(
            authorization_header=request.headers.get("Authorization"),
            cookie_token=request.cookies.get(COOKIE_NAME),
        ):
            return await call_next(request)
        return JSONResponse({"detail": "unauthorized"}, status_code=401)


def create_app(
    *,
    auth: GatewayAuth,
    node_name: str,
    spa_assets_dir: Optional[Path] = None,
) -> FastAPI:
    app = FastAPI(title="turing-gateway")
    app.state.start_time = time.monotonic()
    app.add_middleware(_BearerMiddleware, auth=auth)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/token-handoff")
    async def token_handoff(token: str = "") -> Response:
        """One-shot ``?token=...`` → cookie + redirect to ``/``.

        The launcher opens this URL in the browser; the cookie persists, so
        subsequent navigation to ``/`` succeeds without the operator ever
        seeing the token in the address bar.
        """
        if not token or not auth.check(
            authorization_header=f"Bearer {token}", cookie_token=None
        ):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=False,  # Tailscale-internal; HTTPS termination optional
            samesite="lax",
        )
        return response

    @app.get("/static-stub")
    async def static_stub() -> dict:
        # Kept for slice-3 tests that exercise the auth middleware without
        # depending on the SPA bundle being present.
        return {"ok": True}

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        if not auth.check(
            authorization_header=websocket.headers.get("Authorization"),
            cookie_token=websocket.cookies.get(COOKIE_NAME),
        ):
            await websocket.close(code=1008)  # Policy violation
            return
        await websocket.accept()
        uptime_s = time.monotonic() - app.state.start_time
        hello = {"type": "hello", "node_name": node_name, "uptime_s": uptime_s}
        await websocket.send_text(json.dumps(hello))
        try:
            while True:
                await websocket.receive_text()
        except Exception:
            return

    if spa_assets_dir is not None and Path(spa_assets_dir).is_dir():
        index_path = Path(spa_assets_dir) / "index.html"

        @app.get("/", include_in_schema=False)
        async def spa_index() -> Response:
            if not index_path.is_file():
                return JSONResponse({"detail": "spa not built"}, status_code=404)
            return FileResponse(index_path, media_type="text/html")

        # Mount everything else under the assets dir at the root path. The
        # /-route handler above takes precedence for the literal "/".
        app.mount(
            "/",
            StaticFiles(directory=str(spa_assets_dir), html=False),
            name="spa-static",
        )

    return app
