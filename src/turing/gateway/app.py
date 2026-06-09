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
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
)
from starlette.staticfiles import StaticFiles

from turing.coordinator.alerts.types import KNOWN_FIELDS
from turing.gateway.auth import COOKIE_NAME, GatewayAuth
from turing.gateway.chat_manager import (
    ChatSessionNotFoundError,
    ChatSubtaskNotFoundError,
    ChatSubtaskNotRewardableError,
)
from turing.gateway.queue_manager import QueueItemNotFoundError
from turing.mesh.node import is_specs_stale

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from turing.coordinator.alerts.dispatcher import AlertDispatcher
    from turing.coordinator.alerts.types import Field
    from turing.gateway.chat_manager import ChatManager
    from turing.gateway.queue_manager import QueueManager
    from turing.gateway.ring_buffer import RingBuffer
    from turing.gateway.telemetry_sink import TelemetrySink
    from turing.mesh.node import MeshNode

DEFAULT_PAGE_LIMIT = 200

# Upper bound on a single ``/api/events`` page; without it a caller can request
# an arbitrarily large ``limit`` and force a full ring-buffer scan + JSON build.
MAX_PAGE_LIMIT = 1000

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


def _ws_origin_allowed(websocket: WebSocket) -> bool:
    """Allow the WS handshake only for same-origin or non-browser callers.

    Browsers always send an ``Origin`` header on a WebSocket handshake; if it
    is present it must match the ``Host`` the request was sent to. Requests
    with no ``Origin`` (curl, server-side clients) are allowed through to the
    bearer check. This blocks cross-site WebSocket hijacking via the cookie.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    host = websocket.headers.get("host")
    if not host:
        return False
    return urlsplit(origin).netloc == host


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
    queue_manager: QueueManager | None = None,
    chat_manager: ChatManager | None = None,
) -> FastAPI:
    app = FastAPI(title="turing-gateway")
    app.state.start_time = time.monotonic()
    app.state.ring_buffer = ring_buffer
    app.state.telemetry_sink = telemetry_sink
    app.state.alert_dispatcher = alert_dispatcher
    app.state.queue_manager = queue_manager
    app.state.chat_manager = chat_manager
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
        # Clamp pagination params so a single request can't force a full-table
        # scan + JSON materialization of the entire buffer (DoS / memory spike).
        limit = min(max(0, int(limit)), MAX_PAGE_LIMIT)
        offset = max(0, int(offset))
        # Fetch one extra row to detect whether more pages remain.
        fetch_limit = limit + 1
        rows = await ring_buffer.query(
            node_names=tuple(node) if node else None,
            event_types=tuple(event_type) if event_type else None,
            since_ms=since_ms,
            min_duration_ms=min_duration_ms,
            limit=fetch_limit,
            offset=offset,
        )
        has_more = len(rows) > limit
        events = rows[:limit]
        next_offset = (offset + limit) if has_more else None
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

    # ── Question-queue manager (ADR 0010 Slice C) ────────────────────────────
    #
    # The webui's primary work-direction surface. Reads serve the initial
    # snapshot; the four POSTs drive the human-gated frontier. approve / accept
    # / reject / edit write to ``episode_rewards`` with magnitudes identical to
    # the retired Discord surface (the QueueManager owns the emitter). Every
    # mutation fans a ``queue.delta`` frame out through the telemetry sink, so
    # the bearer-gated WS subscribers stay in sync. All routes are bearer-gated
    # by ``_BearerMiddleware``; a missing item 404s.

    def _require_queue() -> QueueManager:
        if queue_manager is None:
            raise HTTPException(status_code=503, detail="queue manager not enabled")
        return queue_manager

    @app.get("/api/queue")
    async def get_queue() -> dict:
        """Full queue snapshot for initial paint (WS layers deltas on top)."""
        return _require_queue().snapshot_frame()

    @app.post("/api/queue/approve/{item_id}")
    async def approve_question(item_id: str) -> dict:
        """proposed → approved. No reward (approval is not a decision yet)."""
        try:
            item = await _require_queue().approve(item_id)
        except QueueItemNotFoundError as exc:
            raise HTTPException(status_code=404, detail="unknown queue item") from exc
        return item.to_frame()

    @app.post("/api/queue/accept/{item_id}")
    async def accept_question(item_id: str) -> dict:
        """Curate-accept the worker draft: +1.0 reward to the episode."""
        try:
            item = await _require_queue().accept(item_id)
        except QueueItemNotFoundError as exc:
            raise HTTPException(status_code=404, detail="unknown queue item") from exc
        return item.to_frame()

    @app.post("/api/queue/reject/{item_id}")
    async def reject_question(item_id: str) -> dict:
        """Curate-reject the worker draft: −1.0 reward to the episode."""
        try:
            item = await _require_queue().reject(item_id)
        except QueueItemNotFoundError as exc:
            raise HTTPException(status_code=404, detail="unknown queue item") from exc
        return item.to_frame()

    @app.post("/api/queue/edit/{item_id}")
    async def edit_question(item_id: str, corrected_answer: str = Body(..., embed=True)) -> dict:
        """Curate-edit: capture the operator's correction; +0.3 partial reward."""
        try:
            item = await _require_queue().edit(item_id, corrected_answer=corrected_answer)
        except QueueItemNotFoundError as exc:
            raise HTTPException(status_code=404, detail="unknown queue item") from exc
        return item.to_frame()

    # ── Chat pane (ADR 0010 Slice E) ─────────────────────────────────────────
    #
    # The webui's secondary, ad-hoc work-direction surface. The operator submits
    # a free-form prompt; the coordinator plans a DAG; subtasks stream back into
    # the same thread; per-subtask thumbs write ``episode_rewards`` REUSING Slice
    # C's emitter (the ChatManager delegates to a QueueManager over the shared
    # store — no new reward path). Like the queue routes, every mutation fans a
    # ``chat.delta`` out through the telemetry sink; all routes are bearer-gated;
    # an unknown session/subtask 404s; a missing manager 503s.

    def _require_chat() -> ChatManager:
        if chat_manager is None:
            raise HTTPException(status_code=503, detail="chat manager not enabled")
        return chat_manager

    @app.get("/api/chat")
    async def get_chat() -> dict:
        """Full chat-session snapshot for initial paint (WS layers deltas on top)."""
        return _require_chat().snapshot_frame()

    @app.post("/api/chat/submit")
    async def submit_chat(
        prompt: str = Body(..., embed=True),
        session_id: str | None = Body(default=None, embed=True),
        specialty: str = Body(default="research", embed=True),
    ) -> dict:
        """Open an ad-hoc chat thread for ``prompt``.

        The coordinator plans the DAG and streams subtasks back over the WS;
        this endpoint just records the thread and returns its session. A
        client-supplied ``session_id`` makes the submit idempotent on retry;
        absent one, the gateway mints a time-based id.
        """
        chat = _require_chat()
        sid = session_id or f"chat-{int(time.time() * 1000)}"
        session = await chat.submit(session_id=sid, prompt=prompt, specialty=specialty)
        return session.to_frame()

    @app.post("/api/chat/{session_id}/{subtask_id}/accept")
    async def accept_subtask(session_id: str, subtask_id: str) -> dict:
        """Thumb-up a streamed subtask: +1.0 reward to its episode."""
        try:
            subtask = await _require_chat().accept(session_id, subtask_id)
        except (ChatSessionNotFoundError, ChatSubtaskNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="unknown chat subtask") from exc
        except ChatSubtaskNotRewardableError as exc:
            raise HTTPException(status_code=409, detail="subtask not rewardable") from exc
        return subtask.to_frame()

    @app.post("/api/chat/{session_id}/{subtask_id}/reject")
    async def reject_subtask(session_id: str, subtask_id: str) -> dict:
        """Thumb-down a streamed subtask: −1.0 reward to its episode."""
        try:
            subtask = await _require_chat().reject(session_id, subtask_id)
        except (ChatSessionNotFoundError, ChatSubtaskNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="unknown chat subtask") from exc
        except ChatSubtaskNotRewardableError as exc:
            raise HTTPException(status_code=409, detail="subtask not rewardable") from exc
        return subtask.to_frame()

    @app.post("/api/chat/{session_id}/{subtask_id}/edit")
    async def edit_subtask(
        session_id: str, subtask_id: str, corrected_answer: str = Body(..., embed=True)
    ) -> dict:
        """Edit a streamed subtask: capture the correction; +0.3 partial reward."""
        try:
            subtask = await _require_chat().edit(
                session_id, subtask_id, corrected_answer=corrected_answer
            )
        except (ChatSessionNotFoundError, ChatSubtaskNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="unknown chat subtask") from exc
        except ChatSubtaskNotRewardableError as exc:
            raise HTTPException(status_code=409, detail="subtask not rewardable") from exc
        return subtask.to_frame()

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        # Reject cross-origin handshakes (CSWSH): the cookie auth below would
        # otherwise let any web page the operator visits open a socket with the
        # ambient gateway cookie and read the full telemetry/queue/chat stream.
        # SameSite=Lax does not reliably cover the WS handshake, so enforce a
        # same-origin check explicitly. Non-browser clients (no Origin header)
        # are allowed; they still need the bearer token.
        if not _ws_origin_allowed(websocket):
            await websocket.close(code=1008)  # Policy violation
            return
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

        # Seed the queue pane with a full snapshot before any deltas, so a
        # freshly-connected client renders the current frontier immediately
        # and then layers live ``queue.delta`` frames on top.
        if queue_manager is not None:
            await websocket.send_text(json.dumps(queue_manager.snapshot_frame()))

        # Same contract for the chat pane (Slice E): a ``chat.snapshot`` on
        # connect, then live ``chat.delta`` frames over the shared fan-out.
        if chat_manager is not None:
            await websocket.send_text(json.dumps(chat_manager.snapshot_frame()))

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
