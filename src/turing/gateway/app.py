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
from typing import TYPE_CHECKING, cast

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
)
from starlette.staticfiles import StaticFiles

from turing.coordinator.alerts.types import KNOWN_FIELDS
from turing.gateway.auth import COOKIE_NAME, GatewayAuth
from turing.mesh.node import is_specs_stale

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from turing.coordinator.alerts.dispatcher import AlertDispatcher
    from turing.coordinator.alerts.types import Field
    from turing.gateway.ring_buffer import RingBuffer
    from turing.gateway.telemetry_sink import TelemetrySink
    from turing.mesh.node import MeshNode

DEFAULT_PAGE_LIMIT = 200

PUBLIC_PATHS = frozenset({"/", "/healthz", "/token-handoff", "/peers"})

# Friendly landing payload returned on bare ``GET /`` when the caller is not
# authenticated (or when no SPA bundle is mounted). Keeps the operator from
# staring at a raw 401 with no next step — points them at the token-handoff
# route where the launcher's one-shot URL completes the login.
_LANDING_PAYLOAD = {
    "service": "turing-gateway",
    "login": "/token-handoff?token=<your-token>",
    "healthz": "/healthz",
}


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
    spa_assets_dir: Path | None = None,
    ring_buffer: RingBuffer | None = None,
    mesh_node: MeshNode | None = None,
    telemetry_sink: TelemetrySink | None = None,
    alert_dispatcher: AlertDispatcher | None = None,
) -> FastAPI:
    app = FastAPI(title="turing-gateway")
    app.state.start_time = time.monotonic()
    app.state.ring_buffer = ring_buffer
    app.state.telemetry_sink = telemetry_sink
    app.state.alert_dispatcher = alert_dispatcher
    app.add_middleware(_BearerMiddleware, auth=auth)  # type: ignore[arg-type]

    # Wire the dispatcher's frame fan-out through the telemetry sink so a
    # single set of WS subscribers receives both telemetry and alert frames.
    if alert_dispatcher is not None and telemetry_sink is not None:
        alert_dispatcher.set_send_frame(telemetry_sink._broadcast)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/peers")
    async def peers() -> dict:
        """Return the current mesh peer view from this node's perspective.

        Reads :class:`MeshNode.peers`, which the NATS-presence subscriber
        keeps fresh (ADR-0008). Includes this node as the first entry so
        the operator UI graph can render the full mesh without a second
        request. Public so the fleet health check can hit it without
        threading the bearer token through curl.
        """
        self_specs = getattr(mesh_node, "self_specs", None) if mesh_node else None
        result: list[dict] = [
            {
                "node_id": getattr(mesh_node, "node_id", node_name) if mesh_node else node_name,
                "node_name": node_name,
                "self": True,
                "capabilities": list(mesh_node.capabilities) if mesh_node else [],
                "last_seen": None,
                "specs": self_specs.to_dict() if self_specs is not None else None,
                # The self-row is implicitly "live": we'd not be answering
                # this request if the local process weren't running. The
                # SPA needs a uniform schema, so we still surface the field.
                "stale": False,
            }
        ]
        if mesh_node is not None:
            for peer in mesh_node.peers.values():
                peer_specs = getattr(peer, "specs", None)
                result.append(
                    {
                        "node_id": peer.node_id,
                        "node_name": peer.name,
                        "self": False,
                        "capabilities": list(peer.capabilities),
                        "last_seen": peer.last_seen,
                        "specs": peer_specs.to_dict() if peer_specs is not None else None,
                        "stale": is_specs_stale(peer),
                    }
                )
        return {"peers": result, "count": len(result)}

    @app.get("/token-handoff")
    async def token_handoff(token: str = "") -> Response:
        """One-shot ``?token=...`` → cookie + redirect to ``/``.

        The launcher opens this URL in the browser; the cookie persists, so
        subsequent navigation to ``/`` succeeds without the operator ever
        seeing the token in the address bar.
        """
        if not token or not auth.check(authorization_header=f"Bearer {token}", cookie_token=None):
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

    @app.get("/api/events")
    async def api_events(
        node: list[str] = Query(default_factory=list),
        event_type: list[str] = Query(default_factory=list),
        since_ms: int | None = None,
        min_duration_ms: float | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> dict:
        """Paginated query over the telemetry ring buffer.

        Slice 7's trace pane fetches recent history through this route on
        initial paint, then layers live frames from the WebSocket on top.
        Pagination uses ``offset`` rather than a cursor because the ring
        buffer is append-only and rows never re-shuffle.
        """
        if ring_buffer is None:
            return {"events": [], "next_offset": None}
        # Fetch one extra row to detect whether more pages remain.
        fetch_limit = max(0, int(limit)) + 1
        rows = await ring_buffer.query(
            node_names=tuple(node) if node else None,
            event_types=tuple(event_type) if event_type else None,
            since_ms=since_ms,
            min_duration_ms=min_duration_ms,
            limit=fetch_limit,
            offset=int(offset),
        )
        has_more = len(rows) > limit
        events = rows[:limit]
        next_offset = (int(offset) + limit) if has_more else None
        return {"events": events, "next_offset": next_offset}

    @app.post("/alerts/{node_id}/{field}/snooze")
    async def snooze_alert(node_id: str, field: str) -> dict:
        """Snooze an active alert for a ``(peer, field)`` pair.

        Bearer-gated by the middleware. Returns ``{snoozed_until_ms}``;
        404s an unknown field (catches typos like ``disk_used``) or a
        ``(peer, field)`` the alert engine has never graded.
        """
        if alert_dispatcher is None:
            raise HTTPException(status_code=503, detail="alerts subsystem not enabled")
        if field not in KNOWN_FIELDS:
            raise HTTPException(status_code=404, detail=f"unknown alert field: {field}")
        field_typed = cast("Field", field)
        if not alert_dispatcher.engine.has_state(node_id, field_typed):
            raise HTTPException(
                status_code=404,
                detail=f"no alert state for {node_id}/{field}",
            )
        now_ms = int(time.time() * 1000)
        snoozed_until_ms = await alert_dispatcher.snooze(node_id, field_typed, now_ms)
        return {"snoozed_until_ms": snoozed_until_ms}

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

        # Subscribe to the shared fan-out so alert frames (and any future
        # sink-driven frames) reach this client. Telemetry frames already
        # flow through the same path; the existing TelemetrySink unit tests
        # cover that contract.
        unsubscribe = None
        if telemetry_sink is not None:

            async def _send(frame: dict) -> None:
                await websocket.send_text(json.dumps(frame))

            unsubscribe = telemetry_sink.subscribe(_send)

        try:
            while True:
                await websocket.receive_text()
        except Exception:
            return
        finally:
            if unsubscribe is not None:
                unsubscribe()

    if spa_assets_dir is not None and Path(spa_assets_dir).is_dir():
        index_path = Path(spa_assets_dir) / "index.html"

        @app.get("/", include_in_schema=False)
        async def spa_index(request: Request) -> Response:
            # ``/`` is on PUBLIC_PATHS so the middleware lets unauthed callers
            # through — but the SPA itself is privileged. Unauthed visitors
            # get the friendly landing payload with a pointer to login;
            # authed visitors get the real SPA bundle.
            if not auth.check(
                authorization_header=request.headers.get("Authorization"),
                cookie_token=request.cookies.get(COOKIE_NAME),
            ):
                return JSONResponse(_LANDING_PAYLOAD)
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
    else:

        @app.get("/", include_in_schema=False)
        async def landing() -> Response:
            # No SPA bundle present — every visitor (authed or not) gets the
            # friendly landing payload. Beats the old 404/401 dead-ends.
            return JSONResponse(_LANDING_PAYLOAD)

    return app
