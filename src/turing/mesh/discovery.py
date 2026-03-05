"""Peer discovery for the mesh network using Pyre (Python Zyre binding).

Falls back gracefully when Pyre is not installed, logging a warning and
providing a no-op discovery service.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from turing.mesh.node import MeshNode, PeerInfo

logger = structlog.get_logger("turing.mesh.discovery")

# Attempt to import Pyre.  It may not be available on all platforms.
try:
    from pyre import Pyre  # type: ignore[import-untyped]

    PYRE_AVAILABLE = True
except ImportError:
    PYRE_AVAILABLE = False


class PeerDiscovery:
    """UDP-based peer discovery using the Pyre/Zyre protocol.

    When Pyre is not available, discovery is disabled and the node operates
    in standalone mode.
    """

    DISCOVERY_GROUP = "turing-mesh"

    def __init__(self, node: MeshNode, config: Any) -> None:
        self._node = node
        self._config = config
        self._pyre: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the peer discovery service.

        If Pyre is not installed, the service starts in degraded (no-op) mode
        and logs a warning.
        """
        if self._running:
            logger.warning("discovery_already_running")
            return

        if not PYRE_AVAILABLE:
            logger.warning(
                "pyre_not_available",
                msg="Pyre library not installed. Peer discovery disabled. "
                "Install pyre2 to enable mesh networking.",
            )
            self._running = True
            return

        try:
            self._pyre = Pyre(self._node.node_name)

            # Advertise node identity and capabilities as headers.
            self._pyre.set_header("node_id", self._node.node_id)
            self._pyre.set_header("node_name", self._node.node_name)
            self._pyre.set_header("capabilities", json.dumps(self._node.capabilities))

            self._pyre.join(self.DISCOVERY_GROUP)
            self._pyre.start()

            self._running = True
            self._task = asyncio.get_running_loop().create_task(self._discovery_loop())

            logger.info(
                "discovery_started",
                node_name=self._node.node_name,
                group=self.DISCOVERY_GROUP,
            )
        except Exception as exc:
            logger.error("discovery_start_failed", error=str(exc))
            self._running = False

    async def stop(self) -> None:
        """Stop the peer discovery service."""
        self._running = False

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._pyre is not None:
            try:
                self._pyre.leave(self.DISCOVERY_GROUP)
                self._pyre.stop()
            except Exception as exc:
                logger.error("discovery_stop_error", error=str(exc))
            self._pyre = None

        logger.info("discovery_stopped")

    async def _discovery_loop(self) -> None:
        """Background task that polls Pyre for peer events.

        Runs in a loop using ``run_in_executor`` to avoid blocking the
        event loop, since the Pyre socket is synchronous.
        """
        loop = asyncio.get_running_loop()

        while self._running and self._pyre is not None:
            try:
                # Poll with a short timeout to keep the loop responsive.
                event = await loop.run_in_executor(None, self._poll_pyre)
                if event is not None:
                    await self._handle_event(event)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("discovery_loop_error", error=str(exc))
                await asyncio.sleep(1.0)

    def _poll_pyre(self) -> dict[str, Any] | None:
        """Synchronous poll of the Pyre socket.

        Returns a dict describing the event, or None if no event was received.
        """
        if self._pyre is None:
            return None

        try:
            # recv() returns a list: [event_type, peer_uuid, peer_name, ...]
            # Use a poller or non-blocking recv with timeout.
            poller = self._pyre.socket().poll(timeout=500)  # 500ms
            if not poller:
                return None

            msg = self._pyre.recv()
            if not msg:
                return None

            event_type = msg[0].decode("utf-8") if isinstance(msg[0], bytes) else str(msg[0])
            peer_uuid = msg[1].decode("utf-8") if isinstance(msg[1], bytes) else str(msg[1])
            peer_name = msg[2].decode("utf-8") if isinstance(msg[2], bytes) else str(msg[2])

            return {
                "type": event_type,
                "peer_uuid": peer_uuid,
                "peer_name": peer_name,
                "headers": msg[3] if len(msg) > 3 else None,
                "message": msg[4] if len(msg) > 4 else None,
            }
        except Exception:
            return None

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Process a Pyre discovery event."""
        event_type = event.get("type", "")
        peer_uuid = event.get("peer_uuid", "")
        peer_name = event.get("peer_name", "")

        if event_type == "ENTER":
            # Parse capabilities from headers if available.
            headers = event.get("headers")
            capabilities: list[str] = []
            node_id = peer_uuid

            if headers and isinstance(headers, dict):
                caps_json = headers.get("capabilities", "[]")
                try:
                    capabilities = json.loads(caps_json) if isinstance(caps_json, str) else []
                except (json.JSONDecodeError, TypeError):
                    capabilities = []
                node_id = headers.get("node_id", peer_uuid)

            peer = PeerInfo(
                node_id=node_id,
                name=peer_name,
                capabilities=capabilities,
            )
            self._node.add_peer(peer)
            logger.info(
                "peer_entered",
                peer_uuid=peer_uuid,
                peer_name=peer_name,
                capabilities=capabilities,
            )

        elif event_type == "EXIT":
            self._node.remove_peer(peer_uuid)
            logger.info("peer_exited", peer_uuid=peer_uuid, peer_name=peer_name)

        elif event_type == "SHOUT" or event_type == "WHISPER":
            # Handle incoming messages via the mesh client/protocol.
            logger.debug(
                "peer_message",
                type=event_type,
                peer_uuid=peer_uuid,
                peer_name=peer_name,
            )
