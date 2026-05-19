"""Mesh node representing this Pi's identity in the peer-to-peer network."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from turing.specs.collector import NodeSpecs

logger = structlog.get_logger("turing.mesh.node")

# Matches PresenceService.DEFAULT_STALE_AFTER (ADR-0008). Duplicated as a
# module-level constant so ``is_specs_stale`` can be imported by the gateway
# without dragging in the presence service.
STALE_AFTER_SECONDS = 60.0


def is_specs_stale(
    peer: PeerInfo,
    *,
    now: float | None = None,
    stale_after: float = STALE_AFTER_SECONDS,
) -> bool:
    """Return ``True`` when this peer's last heartbeat is older than the window.

    Pure function with injectable ``now`` so the gateway/UI tests can pin
    a deterministic timestamp instead of monkey-patching ``time.time``.
    """
    reference = now if now is not None else time.time()
    return (reference - peer.last_seen) > stale_after


@dataclass
class PeerInfo:
    """Information about a discovered peer node."""

    node_id: str
    name: str
    capabilities: list[str] = field(default_factory=list)
    last_seen: float = field(default_factory=time.time)
    address: str = ""
    # Per-peer live specs (CPU%, temperature, ...). ``None`` for peers
    # running an older build that pre-dates the specs panel slice.
    specs: NodeSpecs | None = None

    @property
    def is_stale(self) -> bool:
        """Return True if the peer has not been seen for more than 60 seconds."""
        return (time.time() - self.last_seen) > 60.0

    def touch(self) -> None:
        """Update the last_seen timestamp to now."""
        self.last_seen = time.time()


class MeshNode:
    """Represents this Pi's identity and state within the mesh network.

    Tracks known peers and advertises the local node's capabilities
    (the list of available tool names).
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self._node_name: str = getattr(config, "node_name", "unknown")
        self._node_id: str = getattr(config, "node_id", "unknown")
        self._capabilities: list[str] = []
        self._peers: dict[str, PeerInfo] = {}
        self._running: bool = False
        # Live self-row specs, refreshed by ``PresenceService._publish_heartbeat``
        # so the gateway's ``/peers`` self-row reports current values without
        # a second sample. ``None`` before the first heartbeat.
        self._self_specs: NodeSpecs | None = None

    @property
    def node_name(self) -> str:
        """Human-readable name for this node."""
        return self._node_name

    @property
    def node_id(self) -> str:
        """Unique identifier for this node."""
        return self._node_id

    @property
    def capabilities(self) -> list[str]:
        """List of tool names available on this node."""
        return list(self._capabilities)

    @capabilities.setter
    def capabilities(self, value: list[str]) -> None:
        self._capabilities = list(value)

    @property
    def peers(self) -> dict[str, PeerInfo]:
        """Dictionary of known peers keyed by their node_id."""
        return dict(self._peers)

    @property
    def self_specs(self) -> NodeSpecs | None:
        return self._self_specs

    @self_specs.setter
    def self_specs(self, value: NodeSpecs | None) -> None:
        self._self_specs = value

    @property
    def is_running(self) -> bool:
        """Whether the mesh node is currently active."""
        return self._running

    async def start(self) -> None:
        """Start the mesh node."""
        if self._running:
            logger.warning("mesh_node_already_running", node_id=self._node_id)
            return
        self._running = True
        logger.info(
            "mesh_node_started",
            node_name=self._node_name,
            node_id=self._node_id,
            capabilities=self._capabilities,
        )

    async def stop(self) -> None:
        """Stop the mesh node and clear peer list."""
        if not self._running:
            return
        self._running = False
        self._peers.clear()
        logger.info("mesh_node_stopped", node_id=self._node_id)

    def add_peer(self, peer: PeerInfo) -> None:
        """Add or update a peer in the known peers dictionary."""
        peer.touch()
        self._peers[peer.node_id] = peer
        logger.info(
            "peer_added",
            peer_id=peer.node_id,
            peer_name=peer.name,
            capabilities=peer.capabilities,
        )

    def remove_peer(self, node_id: str) -> PeerInfo | None:
        """Remove a peer from the known peers dictionary.

        Returns the removed PeerInfo, or None if the peer was not found.
        """
        peer = self._peers.pop(node_id, None)
        if peer is not None:
            logger.info("peer_removed", peer_id=node_id, peer_name=peer.name)
        return peer

    def get_peer(self, node_id: str) -> PeerInfo | None:
        """Get a peer by node_id."""
        return self._peers.get(node_id)

    def prune_stale_peers(self) -> list[str]:
        """Remove peers that have not been seen recently.

        Returns the list of removed peer IDs.
        """
        stale = [pid for pid, peer in self._peers.items() if peer.is_stale]
        for pid in stale:
            self.remove_peer(pid)
        return stale
