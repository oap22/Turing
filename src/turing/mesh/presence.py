"""NATS-backed mesh peer presence (replaces Pyre/Zyre discovery).

Each node publishes a signed heartbeat to ``mesh.presence.heartbeat`` every
``heartbeat_interval`` seconds and subscribes to both heartbeats and
``mesh.presence.leave`` messages from peers. A background prune task evicts
peers whose last heartbeat is older than ``stale_after`` seconds, matching
the existing :meth:`MeshNode.prune_stale_peers` 60s window.

Since issue #348 presence rides :class:`SignedTransport`, not the raw bus:
every heartbeat/leave is an Ed25519-signed ``MeshMessage`` envelope, verified
against the per-node trusted-keys map, with replay protection on
``request_id``. Peer identity comes from the *verified* ``sender_id`` — a
payload claiming a different ``node_id`` is rejected, and a ``leave`` can
only ever evict the sender itself. Unsigned/garbage frames are dropped via
the transport's ``on_error`` hook. A per-sender rate-limit floor caps how
fast accepted heartbeats from one node can churn the peer table.

See ADR-0008 for the decision context. The public shape mirrors the legacy
``PeerDiscovery`` (``start`` / ``stop`` / ``is_running``) so the boot
sequence in ``__main__`` can swap implementations cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from turing.llm.endpoints import validate_ollama_endpoint
from turing.mesh.node import MeshNode, PeerInfo
from turing.specs.collector import NodeSpecs, collect_specs
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport, UntrustedSenderError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from turing.coordinator.alerts.dispatcher import AlertDispatcher

logger = structlog.get_logger("turing.mesh.presence")

HEARTBEAT_SUBJECT = "mesh.presence.heartbeat"
LEAVE_SUBJECT = "mesh.presence.leave"

# ADR-0008: 10s heartbeat, 60s stale threshold (6x cadence absorbs one
# transient NATS hiccup before eviction).
DEFAULT_HEARTBEAT_INTERVAL = 10.0
DEFAULT_STALE_AFTER = 60.0

# Issue #348: per-sender floor between *accepted* heartbeats. Production
# cadence is 10s, so 1s tolerates aggressive restarts while stopping a
# trusted-but-misbehaving node from spinning the peer table / alert engine.
DEFAULT_HEARTBEAT_MIN_INTERVAL = 1.0

# v3 bump: presence moved from raw JSON on the bus to signed MeshMessage
# envelopes (issue #348). v2 added the ``specs`` block (#215). Unsigned v1/v2
# nodes are no longer interoperable — their frames fail envelope decoding and
# are dropped (fail-closed; mixed fleets must upgrade together).
# The ``models`` / ``ollama_host`` fields (peer model routing) are additive
# and optional, so they ride v3 unchanged: a v3 node without them simply
# advertises no models.
SCHEMA_VERSION = 3

# The pulled-model list changes when an operator runs `ollama pull`, not
# every 10 s; re-asking Ollama on every heartbeat would be pointless load
# on the node that can least afford it.
MODEL_SAMPLE_INTERVAL = 60.0


class PresenceService:
    """Publish/subscribe peer-presence service on the signed mesh transport."""

    def __init__(
        self,
        node: MeshNode,
        transport: SignedTransport,
        *,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        stale_after: float = DEFAULT_STALE_AFTER,
        heartbeat_min_interval: float = DEFAULT_HEARTBEAT_MIN_INTERVAL,
        alert_dispatcher: AlertDispatcher | None = None,
    ) -> None:
        self._node = node
        self._transport = transport
        self._heartbeat_interval = heartbeat_interval
        self._stale_after = stale_after
        self._heartbeat_min_interval = heartbeat_min_interval
        self._alert_dispatcher = alert_dispatcher
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._prune_task: asyncio.Task[None] | None = None
        self._running = False
        # Verified sender_id -> monotonic time of the last ACCEPTED heartbeat.
        # Bounded by the trusted-keys set: only verified senders ever land here.
        self._last_accepted: dict[str, float] = {}
        # Running count of frames the transport rejected (bad signature,
        # untrusted/unbound sender, replay, garbage bytes).
        self._rejected_count = 0
        # Optional async sampler of this node's pulled models (the local
        # Ollama provider's ``list_models``), cached between samples.
        self._model_sampler: Callable[[], Awaitable[list[str]]] | None = None
        self._models_sampled_at: float | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def rejected_count(self) -> int:
        """Frames dropped by signature/replay verification since start."""
        return self._rejected_count

    def set_alert_dispatcher(self, dispatcher: AlertDispatcher | None) -> None:
        """Late-bind the hardware-safety alert dispatcher.

        The boot sequence builds the dispatcher later than ``PresenceService``
        is constructed (it depends on the gateway telemetry sink), so it is
        attached here rather than passed to ``__init__``.
        """
        self._alert_dispatcher = dispatcher

    def set_model_sampler(self, sampler: Callable[[], Awaitable[list[str]]] | None) -> None:
        """Late-bind the pulled-model sampler advertised in heartbeats."""
        self._model_sampler = sampler
        self._models_sampled_at = None

    async def _sample_self_models(self) -> list[str]:
        """Refresh ``node.self_models`` at most every ``MODEL_SAMPLE_INTERVAL``.

        A sampler failure keeps the last known list: a transient Ollama
        hiccup must not make peers believe our models vanished.
        """
        if self._model_sampler is None:
            return self._node.self_models
        now = time.monotonic()
        if (
            self._models_sampled_at is not None
            and (now - self._models_sampled_at) < MODEL_SAMPLE_INTERVAL
        ):
            return self._node.self_models
        try:
            self._node.self_models = sorted(set(await self._model_sampler()))
        except Exception:
            logger.warning("presence_model_sample_failed", exc_info=True)
        self._models_sampled_at = now
        return self._node.self_models

    async def start(self) -> None:
        if self._running:
            logger.warning("presence_already_running")
            return

        await self._transport.subscribe(
            HEARTBEAT_SUBJECT, self._on_heartbeat, on_error=self._on_verify_error
        )
        await self._transport.subscribe(
            LEAVE_SUBJECT, self._on_leave, on_error=self._on_verify_error
        )

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
        """Stop the service and publish a graceful signed ``leave`` message."""
        if not self._running:
            return
        self._running = False

        with contextlib.suppress(Exception):
            await self._transport.publish(
                self._envelope(LEAVE_SUBJECT, {"node_id": self._node.node_id})
            )

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

    def _envelope(self, subject: str, payload: dict[str, Any]) -> MeshMessage:
        """Wrap a presence payload in a signed-transport envelope.

        A fresh ``request_id`` per message keeps successive heartbeats from
        tripping the transport's replay window — only literal re-deliveries
        of a captured frame share a ``request_id``.
        """
        return MeshMessage(
            request_id=uuid.uuid4().hex,
            sender_id=self._node.node_id,
            subject=subject,
            payload=json.dumps(payload).encode("utf-8"),
            timestamp_ms=int(time.time() * 1000),
        )

    async def _publish_heartbeat(self) -> None:
        specs = self._sample_self_specs()
        # Mirror self-specs onto the MeshNode so the gateway's ``/peers``
        # self-row reflects live values without a second sample.
        self._node.self_specs = specs
        models = await self._sample_self_models()
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "node_id": self._node.node_id,
            "node_name": self._node.node_name,
            "capabilities": self._node.capabilities,
            "ts_ms": int(time.time() * 1000),
            "specs": specs.to_dict() if specs is not None else None,
            "models": models,
            "ollama_host": self._node.ollama_host,
        }
        await self._transport.publish(self._envelope(HEARTBEAT_SUBJECT, payload))

    def _on_verify_error(self, exc: Exception) -> None:
        """Count and log frames the transport refused to deliver.

        Untrusted/unbound senders are logged at warning (someone is actively
        publishing bad signatures on our subjects); replays and garbage at
        debug (replay is also what a slow redelivery looks like).
        """
        self._rejected_count += 1
        event = "presence_message_rejected"
        fields = {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "rejected_total": self._rejected_count,
        }
        if isinstance(exc, UntrustedSenderError):
            logger.warning(event, **fields)
        else:
            logger.debug(event, **fields)

    async def _on_heartbeat(self, message: MeshMessage) -> None:
        sender_id = message.sender_id  # verified by SignedTransport
        if not sender_id or sender_id == self._node.node_id:
            # Ignore our own heartbeats.
            return
        msg = self._decode(message.payload)
        if msg is None:
            return
        # Identity consistency (issue #348): the peer table is keyed by the
        # VERIFIED sender_id. A payload claiming someone else's node_id is a
        # spoof attempt (or a badly misconfigured node) — reject outright.
        claimed = msg.get("node_id")
        if claimed is not None and str(claimed) != sender_id:
            logger.warning(
                "presence_identity_mismatch",
                sender_id=sender_id,
                claimed_node_id=claimed,
            )
            return
        # Per-sender rate-limit floor on accepted heartbeats.
        now = time.monotonic()
        last = self._last_accepted.get(sender_id)
        if last is not None and (now - last) < self._heartbeat_min_interval:
            logger.debug(
                "presence_heartbeat_rate_limited",
                sender_id=sender_id,
                interval=now - last,
                floor=self._heartbeat_min_interval,
            )
            return
        self._last_accepted[sender_id] = now

        raw_specs = msg.get("specs")
        parsed_specs: NodeSpecs | None
        if isinstance(raw_specs, dict):
            try:
                parsed_specs = NodeSpecs.from_dict(raw_specs)
            except (TypeError, ValueError):
                parsed_specs = None
        else:
            parsed_specs = None
        raw_models = msg.get("models")
        models = (
            [str(m) for m in raw_models if isinstance(m, str)]
            if isinstance(raw_models, list)
            else []
        )
        raw_host = msg.get("ollama_host")
        ollama_host: str | None = None
        if raw_host is not None:
            try:
                validated_host = validate_ollama_endpoint(raw_host, field="peer Ollama endpoint")
            except ValueError:
                logger.warning("presence_peer_endpoint_rejected", sender_id=sender_id)
            else:
                if validated_host in self._node.ollama_peer_allowlist:
                    ollama_host = validated_host
                else:
                    logger.warning(
                        "presence_peer_endpoint_not_allowlisted",
                        sender_id=sender_id,
                        host=validated_host,
                    )
        peer = PeerInfo(
            node_id=sender_id,
            name=str(msg.get("node_name", sender_id)),
            capabilities=list(msg.get("capabilities", []) or []),
            specs=parsed_specs,
            models=models,
            ollama_host=ollama_host,
        )
        self._node.add_peer(peer)
        if self._alert_dispatcher is not None:
            now_ms = int(time.time() * 1000)
            try:
                await self._alert_dispatcher.observe(peer, now_ms)
            except Exception:  # pragma: no cover — dispatcher swallows internally
                logger.warning("alert_dispatcher_observe_failed", exc_info=True)

    async def _on_leave(self, message: MeshMessage) -> None:
        sender_id = message.sender_id  # verified by SignedTransport
        if not sender_id or sender_id == self._node.node_id:
            return
        # A leave only ever evicts the verified sender itself — any node_id
        # in the payload is ignored, so A can never evict B (issue #348).
        msg = self._decode(message.payload)
        claimed = msg.get("node_id") if msg is not None else None
        if claimed is not None and str(claimed) != sender_id:
            logger.warning(
                "presence_identity_mismatch",
                sender_id=sender_id,
                claimed_node_id=claimed,
                subject=LEAVE_SUBJECT,
            )
            return
        self._node.remove_peer(sender_id)

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any] | None:
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        return obj
