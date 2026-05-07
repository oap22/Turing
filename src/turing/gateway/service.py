"""GatewayService — runs uvicorn inside the existing turing process.

Slice 3 keeps lifecycle to the minimum: start a background asyncio task that
hosts ``uvicorn.Server``; ``stop()`` flips its ``should_exit`` flag and awaits
the task. No extra threads, no new systemd unit.
"""

from __future__ import annotations

import asyncio

import structlog

from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth

logger = structlog.get_logger(__name__)


class GatewayService:
    def __init__(
        self,
        *,
        token: str,
        bind: str,
        port: int,
        node_name: str,
    ) -> None:
        self._token = token
        self._bind = bind
        self._port = port
        self._node_name = node_name
        self._task: asyncio.Task | None = None
        self._server = None  # type: ignore[var-annotated]

    async def start(self) -> None:
        if self._task is not None:
            return
        import uvicorn

        if not self._token:
            raise RuntimeError(
                "gateway_token is empty; refusing to start an unauthenticated gateway"
            )
        if self._bind == "0.0.0.0":
            logger.warning(
                "gateway_binding_to_all_interfaces",
                bind=self._bind,
                hint="set TURING_GATEWAY_BIND to a Tailscale interface for safety",
            )

        app = create_app(
            auth=GatewayAuth(token=self._token), node_name=self._node_name
        )
        config = uvicorn.Config(
            app,
            host=self._bind,
            port=self._port,
            log_level="info",
            lifespan="off",  # We own the lifecycle.
        )
        self._server = uvicorn.Server(config)
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._server.serve(), name="turing-gateway")
        logger.info("gateway_started", bind=self._bind, port=self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None
        logger.info("gateway_stopped")
