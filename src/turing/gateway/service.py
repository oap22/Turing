"""GatewayService — runs uvicorn inside the existing turing process.

Slice 3 keeps lifecycle to the minimum: start a background asyncio task that
hosts ``uvicorn.Server``; ``stop()`` flips its ``should_exit`` flag and awaits
the task. No extra threads, no new systemd unit.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth
from turing.gateway.spa import spa_assets_path

if TYPE_CHECKING:
    import uvicorn

    from turing.coordinator.alerts.dispatcher import AlertDispatcher
    from turing.gateway.chat_manager import ChatManager
    from turing.gateway.queue_manager import QueueManager
    from turing.gateway.telemetry_sink import TelemetrySink
    from turing.mesh.node import MeshNode

logger = structlog.get_logger(__name__)


class GatewayService:
    def __init__(
        self,
        *,
        token: str,
        bind: str,
        port: int,
        node_name: str,
        mesh_node: MeshNode | None = None,
        telemetry_sink: TelemetrySink | None = None,
        alert_dispatcher: AlertDispatcher | None = None,
        queue_manager: QueueManager | None = None,
        chat_manager: ChatManager | None = None,
    ) -> None:
        self._token = token
        self._bind = bind
        self._port = port
        self._node_name = node_name
        self._mesh_node = mesh_node
        self._telemetry_sink = telemetry_sink
        self._alert_dispatcher = alert_dispatcher
        self._queue_manager = queue_manager
        self._chat_manager = chat_manager
        self._task: asyncio.Task | None = None
        self._server: uvicorn.Server | None = None

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
            auth=GatewayAuth(token=self._token),
            node_name=self._node_name,
            spa_assets_dir=spa_assets_path(),
            mesh_node=self._mesh_node,
            telemetry_sink=self._telemetry_sink,
            alert_dispatcher=self._alert_dispatcher,
            queue_manager=self._queue_manager,
            chat_manager=self._chat_manager,
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
            except TimeoutError:
                self._task.cancel()
            self._task = None
        logger.info("gateway_stopped")
