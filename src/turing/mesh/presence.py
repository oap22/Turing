"""NATS-backed mesh peer presence (replaces Pyre/Zyre discovery).

Each node publishes a JSON heartbeat to ``mesh.presence.heartbeat`` every
``heartbeat_interval`` seconds and subscribes to both heartbeats and
``mesh.presence.leave`` messages from peers. A background prune task evicts
peers whose last heartbeat is older than ``stale_after`` seconds, matching
the existing :meth:`MeshNode.prune_stale_peers` 60s window.

See ADR-0008 for the decision context. The public shape mirrors the legacy
``PeerDiscovery`` (``start`` / ``stop`` / ``is_running``) so the boot
sequence in ``__main__`` can swap implementations cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from turing.mesh.node import MeshNode, PeerInfo

if TYPE_CHECKING:
    from turing.transport.bus import Bus

logger = structlog.get_logger("turing.mesh.presence")

HEARTBEAT_SUBJECT = "mesh.presence.heartbeat"
LEAVE_SUBJECT = "mesh.presence.leave"

# ADR-0008: 10s heartbeat, 60s stale threshold (6x cadence absorbs one
# transient NATS hiccup before eviction).
DEFAULT_HEARTBEAT_INTERVAL = 10.0
DEFAULT_STALE_AFTER = 60.0

SCHEMA_VERSION = 1


class PresenceService:
    """Publish/subscribe peer-presence service on the shared NATS bus."""

    def __init__(
        self,
        node: MeshNode,
        bus: Bus,
        *,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        stale_after: float = DEFAULT_STALE_AFTER,
    ) -> None:
        self._node = node
        self._bus = bus
        self._heartbeat_interval = heartbeat_interval
        self._stale_after = stale_after
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._prune_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            logger.warning("presence_already_running")
            return

        await self._bus.subscribe(HEARTBEAT_SUBJECT, self._on_heartbeat)
        await self._bus.subscribe(LEAVE_SUBJECT, self._on_leave)

        self._running = True
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="presence-heartbeat"
        )
        self._prune_task = asyncio.create_task(self._prune_loop(), name="presence-prune")

        # Send an immediate heartbeat so peers learn about us without waiting
        # a full interval.
        await self._publish_heartbeat()

        logger.info(
            "presence_started",
            node_id=self._node.node_id,
            node_name=self._node.node_name,
            heartbeat_interval=self._heartbeat_interval,
        )

    async def stop(self) -> None:
        """Stop the service and publish a graceful ``leave`` message."""
        if not self._running:
            return
        self._running = False

        with contextlib.suppress(Exception):
            payload = json.dumps(
                {"node_id": self._node.node_id, "ts_ms": int(time.time() * 1000)}
            ).encode("utf-8")
            await self._bus.publish(LEAVE_SUBJECT, payload)

        await self._cancel_tasks()
        logger.info("presence_stopped", node_id=self._node.node_id)

    async def _shutdown_without_leave(self) -> None:
        """Stop loops without publishing a leave message (test helper)."""
        self._running = False
        await self._cancel_tasks()

    async def _cancel_tasks(self) -> None:
        for task in (self._heartbeat_task, self._prune_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._heartbeat_task = None
        self._prune_task = None

    async def _heartbeat_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._running:
                    break
                with contextlib.suppress(Exception):
                    await self._publish_heartbeat()
        except asyncio.CancelledError:
            pass

    async def _prune_loop(self) -> None:
        # Sweep on roughly the heartbeat cadence so stale peers fall out
        # within one interval of the TTL expiring.
        interval = self._heartbeat_interval
        try:
            while self._running:
                await asyncio.sleep(interval)
                if not self._running:
                    break
                now = time.time()
                stale = [
                    pid
                    for pid, peer in self._node.peers.items()
                    if (now - peer.last_seen) > self._stale_after
                ]
                for pid in stale:
                    self._node.remove_peer(pid)
                if stale:
                    logger.info("presence_pruned", removed=stale)
        except asyncio.CancelledError:
            pass

    async def _publish_heartbeat(self) -> None:
        payload = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "node_id": self._node.node_id,
                "node_name": self._node.node_name,
                "capabilities": self._node.capabilities,
                "ts_ms": int(time.time() * 1000),
            }
        ).encode("utf-8")
        await self._bus.publish(HEARTBEAT_SUBJECT, payload)

    async def _on_heartbeat(self, raw: bytes) -> None:
        msg = self._decode(raw)
        if msg is None:
            return
        node_id = msg.get("node_id")
        if not node_id or node_id == self._node.node_id:
            # Ignore our own heartbeats and malformed messages.
            return
        peer = PeerInfo(
            node_id=str(node_id),
            name=str(msg.get("node_name", node_id)),
            capabilities=list(msg.get("capabilities", []) or []),
        )
        self._node.add_peer(peer)

    async def _on_leave(self, raw: bytes) -> None:
        msg = self._decode(raw)
        if msg is None:
            return
        node_id = msg.get("node_id")
        if not node_id or node_id == self._node.node_id:
            return
        self._node.remove_peer(str(node_id))

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any] | None:
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        return obj
