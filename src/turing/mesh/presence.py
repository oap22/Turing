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
from turing.specs.collector import NodeSpecs, collect_specs

if TYPE_CHECKING:
    from turing.coordinator.alerts.dispatcher import AlertDispatcher
    from turing.transport.bus import Bus

logger = structlog.get_logger("turing.mesh.presence")

HEARTBEAT_SUBJECT = "mesh.presence.heartbeat"
LEAVE_SUBJECT = "mesh.presence.leave"

# ADR-0008: 10s heartbeat, 60s stale threshold (6x cadence absorbs one
# transient NATS hiccup before eviction).
DEFAULT_HEARTBEAT_INTERVAL = 10.0
DEFAULT_STALE_AFTER = 60.0

# v2 bump: heartbeat payload gained the ``specs`` block (#215). Older nodes
# publish v1 (no ``specs``); receivers register them with ``PeerInfo.specs = None``.
SCHEMA_VERSION = 2


class PresenceService:
    """Publish/subscribe peer-presence service on the shared NATS bus."""

    def __init__(
        self,
        node: MeshNode,
        bus: Bus,
        *,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        stale_after: float = DEFAULT_STALE_AFTER,
        alert_dispatcher: AlertDispatcher | None = None,
    ) -> None:
        self._node = node
        self._bus = bus
        self._heartbeat_interval = heartbeat_interval
        self._stale_after = stale_after
        self._alert_dispatcher = alert_dispatcher
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

    def _sample_self_specs(self) -> NodeSpecs | None:
        """Sample this node's live specs for the next heartbeat.

        Isolated so tests can stub it out, and so a collector hiccup
        (e.g. a transient /sys read failure) never sinks the heartbeat.
        """
        try:
            return collect_specs()
        except Exception:  # pragma: no cover — defensive
            logger.warning("specs_collect_failed", exc_info=True)
            return None

    async def _publish_heartbeat(self) -> None:
        specs = self._sample_self_specs()
        # Mirror self-specs onto the MeshNode so the gateway's ``/peers``
        # self-row reflects live values without a second sample.
        self._node.self_specs = specs
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "node_id": self._node.node_id,
            "node_name": self._node.node_name,
            "capabilities": self._node.capabilities,
            "ts_ms": int(time.time() * 1000),
            "specs": specs.to_dict() if specs is not None else None,
        }
        await self._bus.publish(HEARTBEAT_SUBJECT, json.dumps(payload).encode("utf-8"))

    async def _on_heartbeat(self, raw: bytes) -> None:
        msg = self._decode(raw)
        if msg is None:
            return
        node_id = msg.get("node_id")
        if not node_id or node_id == self._node.node_id:
            # Ignore our own heartbeats and malformed messages.
            return
        # Rolling-upgrade tolerance: peers running schema_version=1 send no
        # ``specs`` key at all — register them with ``specs=None`` rather
        # than dropping the heartbeat.
        raw_specs = msg.get("specs")
        parsed_specs: NodeSpecs | None
        if isinstance(raw_specs, dict):
            try:
                parsed_specs = NodeSpecs.from_dict(raw_specs)
            except (TypeError, ValueError):
                parsed_specs = None
        else:
            parsed_specs = None
        peer = PeerInfo(
            node_id=str(node_id),
            name=str(msg.get("node_name", node_id)),
            capabilities=list(msg.get("capabilities", []) or []),
            specs=parsed_specs,
        )
        self._node.add_peer(peer)
        if self._alert_dispatcher is not None:
            now_ms = int(time.time() * 1000)
            try:
                await self._alert_dispatcher.observe(peer, now_ms)
            except Exception:  # pragma: no cover — dispatcher swallows internally
                logger.warning("alert_dispatcher_observe_failed", exc_info=True)

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
