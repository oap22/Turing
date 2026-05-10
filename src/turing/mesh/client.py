"""Mesh client for sending messages and waiting for responses between nodes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from turing.mesh.protocol import MeshMessage, MessageType

if TYPE_CHECKING:
    from turing.mesh.node import MeshNode

logger = structlog.get_logger("turing.mesh.client")


class MeshClient:
    """High-level client for inter-node communication over the mesh network.

    Provides methods to send task requests, query peer status, broadcast
    messages, and wait for responses with timeout support.
    """

    def __init__(self, node: MeshNode) -> None:
        self._node = node
        # Pending response futures keyed by request_id.
        self._pending: dict[str, asyncio.Future[MeshMessage]] = {}
        # Outbound message queue (consumed by the transport layer).
        self._outbox: asyncio.Queue[tuple[str | None, MeshMessage]] = asyncio.Queue()

    async def send_task(self, peer_id: str, task_description: str) -> str:
        """Send a task request to a specific peer.

        Args:
            peer_id: The node_id of the target peer.
            task_description: Human-readable description of the task.

        Returns:
            The request_id that can be used with ``wait_for_response``.
        """
        message = MeshMessage(
            type=MessageType.TASK_REQUEST,
            sender_id=self._node.node_id,
            sender_name=self._node.node_name,
            payload={"task": task_description},
        )

        await self._send(peer_id, message)
        logger.info(
            "task_sent",
            peer_id=peer_id,
            request_id=message.request_id,
            task=task_description[:100],
        )
        return message.request_id

    async def query_status(self, peer_id: str) -> dict[str, Any]:
        """Query the status of a specific peer.

        Sends a STATUS_QUERY and waits for a STATUS_RESPONSE.

        Args:
            peer_id: The node_id of the target peer.

        Returns:
            The payload from the status response, or an error dict on timeout.
        """
        message = MeshMessage(
            type=MessageType.STATUS_QUERY,
            sender_id=self._node.node_id,
            sender_name=self._node.node_name,
        )

        await self._send(peer_id, message)

        try:
            response = await self.wait_for_response(message.request_id, timeout=10.0)
            return response.payload
        except TimeoutError:
            return {"error": "Peer did not respond within timeout", "peer_id": peer_id}

    async def broadcast(self, message: MeshMessage) -> None:
        """Broadcast a message to all known peers.

        Args:
            message: The message to send to all peers.
        """
        # None as peer_id signals a broadcast to the transport layer.
        await self._outbox.put((None, message))
        logger.info(
            "message_broadcast",
            type=message.type.value,
            request_id=message.request_id,
        )

    async def wait_for_response(
        self,
        request_id: str,
        timeout: float = 30.0,
    ) -> MeshMessage:
        """Wait for a response to a specific request.

        Args:
            request_id: The request_id to match against incoming messages.
            timeout: Maximum time to wait in seconds.

        Returns:
            The matching response message.

        Raises:
            asyncio.TimeoutError: If no response is received within the timeout.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[MeshMessage] = loop.create_future()
        self._pending[request_id] = future

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except TimeoutError:
            logger.warning("response_timeout", request_id=request_id, timeout=timeout)
            raise
        finally:
            self._pending.pop(request_id, None)

    def handle_incoming(self, message: MeshMessage) -> None:
        """Handle an incoming message, resolving any pending futures.

        This method should be called by the transport layer whenever a
        message is received from the network.
        """
        request_id = message.request_id

        # If there is a pending future for this request_id, resolve it.
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(message)
            logger.debug(
                "response_received",
                request_id=request_id,
                type=message.type.value,
                sender=message.sender_name,
            )
            return

        # Otherwise, log the unmatched message for potential processing.
        logger.debug(
            "unmatched_message",
            request_id=request_id,
            type=message.type.value,
            sender=message.sender_name,
        )

    async def get_next_outbound(self) -> tuple[str | None, MeshMessage]:
        """Get the next message from the outbox queue.

        Used by the transport layer to consume outbound messages.

        Returns:
            A tuple of (peer_id or None for broadcast, message).
        """
        return await self._outbox.get()

    async def _send(self, peer_id: str, message: MeshMessage) -> None:
        """Enqueue a message for delivery to a specific peer."""
        await self._outbox.put((peer_id, message))

    async def send_heartbeat(self) -> None:
        """Send a heartbeat message to all peers."""
        message = MeshMessage(
            type=MessageType.HEARTBEAT,
            sender_id=self._node.node_id,
            sender_name=self._node.node_name,
            payload={"capabilities": self._node.capabilities},
        )
        await self.broadcast(message)
