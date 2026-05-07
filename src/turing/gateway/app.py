"""FastAPI app factory for the pi-alpha gateway.

Slice 3 keeps the surface deliberately small: a bearer-gated WebSocket that
sends a ``hello`` frame on connect, plus a ``/healthz`` route that bypasses
auth so a load balancer can liveness-probe. Telemetry fan-out lands in
slice 4 once the SQLite ring buffer exists.
"""

from __future__ import annotations

import json
import time
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from turing.gateway.auth import COOKIE_NAME, GatewayAuth

PUBLIC_PATHS = frozenset({"/healthz"})


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


def create_app(*, auth: GatewayAuth, node_name: str) -> FastAPI:
    app = FastAPI(title="turing-gateway")
    app.state.start_time = time.monotonic()
    app.add_middleware(_BearerMiddleware, auth=auth)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/static-stub")
    async def static_stub() -> dict:
        # Stand-in for the static SPA route added in slice 5; useful for the
        # bearer-middleware test surface without dragging in the React build.
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
        # Keep the socket open until the client closes it; future slices will
        # push telemetry frames here.
        try:
            while True:
                await websocket.receive_text()
        except Exception:
            return

    return app
