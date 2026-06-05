"""Standalone gateway entrypoint — ``python -m turing.gateway`` (ADR 0010 §6).

This is the dedicated unit's target for the three-unit coordinator split: the
webui static bundle + WebSocket + chat/queue API hosted in their *own* process,
separate from ``turing-coordinator``. It reuses :class:`GatewayService` (the
same uvicorn wrapper the in-process gateway uses), so the served surface is
byte-identical to the embedded path.

Opt-in only — see the systemd guard
----------------------------------------------------------------------------
A deployed coordinator already runs the gateway IN-PROCESS, bound to
``config.gateway_port`` (``__main__.py`` builds a :class:`GatewayService` when
``config.gateway_enabled`` is true). If this standalone entrypoint
auto-activated, it would DOUBLE-BIND that port and break the coordinator.
Therefore ``scripts/coordinator/turing-gateway.service`` only execs this module
when ``TURING_GATEWAY_STANDALONE`` is set truthy in ``.env.coordinator`` (and
the operator has flipped ``TURING_GATEWAY_ENABLED=false`` on the coordinator to
release the port). Absent that flag the unit holds as a clean ``sleep infinity``
no-op. This module itself does not read the flag — it assumes the operator has
chosen to run it; the guard lives in the unit so the in-process default is
never disrupted.

Known limitation — empty in-memory projections (pre-IPC split)
----------------------------------------------------------------------------
The live queue / chat / telemetry state lives in the **coordinator** process:
the coordinator's :class:`QueueManager` / :class:`ChatManager` are populated by
the nightly dispatcher and the chat-submit flow, and its :class:`TelemetrySink`
is fed by the mesh telemetry stream. This standalone process gets *fresh*
managers over its own (empty) projections and a *fresh* in-memory ring buffer /
telemetry sink, because there is no cross-process IPC channel yet (that is the
follow-on inter-process-comms slice of ADR 0010 §6). Concretely, run standalone:

- ``/api/queue`` and ``/api/chat`` start empty and only reflect mutations made
  through *this* process; they do not mirror the coordinator's live frontier.
- The ``/ws`` endpoint works and serves snapshots/deltas, but live telemetry
  metrics only populate when the gateway runs in-process under the coordinator
  (the sink here has no mesh feed).
- Reward writes still land in this process's :class:`EpisodeRewardsStore`, which
  is itself an in-memory event table (ADR 0006) — a SQLite projection over
  ``db_path`` is layered above out of scope for this slice, same as the queue /
  chat stores. So standalone reward writes are *not* shared with the
  coordinator's store; they are durable only for this process's lifetime.

In short: the standalone gateway faithfully serves the SPA + the API contract,
but its projection state is local until the IPC split lands. ``mesh_node`` is
``None`` here, so ``/peers`` returns just this node's self-row.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from turing.config import TuringConfig
from turing.coordinator.episode_rewards import EpisodeRewardsStore
from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth
from turing.gateway.chat_manager import ChatManager
from turing.gateway.queue_manager import QueueManager
from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig
from turing.gateway.service import GatewayService
from turing.gateway.spa import spa_assets_path
from turing.gateway.telemetry_sink import TelemetrySink
from turing.logging import setup_logging

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = structlog.get_logger("turing.gateway")


@dataclass
class GatewayResources:
    """The standalone gateway's object graph, built but not yet serving.

    Holds the shared reward store, the fresh in-memory projections, and the
    in-memory telemetry sink + ring buffer. ``build_app`` wraps these in a
    FastAPI app (the testable seam — no socket); ``build_assembly`` wraps them in
    a :class:`GatewayService` (the production path). The ring buffer is owned
    here so the runner can close it on shutdown.
    """

    rewards_store: EpisodeRewardsStore
    queue_manager: QueueManager
    chat_manager: ChatManager
    telemetry_sink: TelemetrySink
    ring_buffer: RingBuffer


@dataclass
class GatewayAssembly:
    """A built-but-not-started standalone gateway: its service + owned resources.

    ``main`` calls ``service.start()``; the runner closes ``resources.ring_buffer``
    on shutdown. Tests prefer :func:`build_app` (a :class:`~fastapi.FastAPI` app
    over the same resources) so they never bind a real socket.
    """

    service: GatewayService
    resources: GatewayResources


def _check_token(config: TuringConfig) -> None:
    """Refuse to start without a bearer token.

    Mirrors :meth:`GatewayService.start`'s guard, but raised *before* any
    sockets/resources are opened so the failure is a clean, fast exit.
    """
    if not config.gateway_token:
        raise SystemExit(
            "turing-gateway: TURING_GATEWAY_TOKEN is empty; refusing to start an "
            "unauthenticated gateway. Set a bearer token and retry."
        )


async def build_resources(config: TuringConfig) -> GatewayResources:
    """Build the standalone gateway's object graph (no socket, no app, no server).

    Builds the same graph the in-process path builds in ``turing.__main__``:
    a shared :class:`EpisodeRewardsStore`, fresh :class:`QueueManager` /
    :class:`ChatManager` over it, and a fresh in-memory :class:`RingBuffer` +
    :class:`TelemetrySink`. The projections start empty and the ring buffer is
    in-memory — see the module docstring for why (no IPC to the coordinator yet).
    """
    import time as _time

    # Fresh in-memory ring buffer + telemetry sink. The sink is the WS fan-out
    # hub; in standalone mode it has no mesh feed, so live metrics stay empty
    # (documented limitation) — but the WS endpoint, snapshots and reward-driven
    # deltas all still work.
    ring_buffer = RingBuffer(
        RingBufferConfig(
            path=Path(":memory:"),
            retention_seconds=3600,
            max_bytes=16 * 1024 * 1024,
        )
    )
    await ring_buffer.open()
    telemetry_sink = TelemetrySink(buffer=ring_buffer)

    # The shared reward store. In-process this is the same instance the Discord
    # path and the nightly critic-fallback sweep write to; standalone it is this
    # process's own in-memory event table (no cross-process sharing yet).
    rewards_store = EpisodeRewardsStore()

    def _now_ms() -> int:
        return int(_time.time() * 1000)

    queue_manager = QueueManager(
        rewards=rewards_store,
        broadcast=telemetry_sink._broadcast,
        now_ms=_now_ms,
    )
    chat_manager = ChatManager(
        rewards=rewards_store,
        broadcast=telemetry_sink._broadcast,
        now_ms=_now_ms,
    )
    return GatewayResources(
        rewards_store=rewards_store,
        queue_manager=queue_manager,
        chat_manager=chat_manager,
        telemetry_sink=telemetry_sink,
        ring_buffer=ring_buffer,
    )


async def build_app(config: TuringConfig) -> FastAPI:
    """Assemble the standalone gateway's FastAPI app — the testable seam.

    Builds the resource graph via :func:`build_resources` and wraps it in the
    same :func:`turing.gateway.app.create_app` factory the in-process path uses,
    so a ``TestClient`` over the returned app exercises the real route table,
    bearer middleware, and manager wiring without binding a socket. ``mesh_node``
    is ``None`` (``/peers`` returns just this node's self-row).
    """
    resources = await build_resources(config)
    return create_app(
        auth=GatewayAuth(token=config.gateway_token),
        node_name=config.node_name,
        spa_assets_dir=spa_assets_path(),
        ring_buffer=resources.ring_buffer,
        mesh_node=None,
        telemetry_sink=resources.telemetry_sink,
        alert_dispatcher=None,
        queue_manager=resources.queue_manager,
        chat_manager=resources.chat_manager,
    )


async def build_assembly(config: TuringConfig) -> GatewayAssembly:
    """Assemble the standalone gateway as an unstarted :class:`GatewayService`.

    The production path: builds the resource graph via :func:`build_resources`
    and hands the managers + sink to a :class:`GatewayService` (the same uvicorn
    wrapper the in-process gateway uses). ``main`` then calls ``service.start()``.
    """
    resources = await build_resources(config)
    service = GatewayService(
        token=config.gateway_token,
        bind=config.gateway_bind,
        port=config.gateway_port,
        node_name=config.node_name,
        mesh_node=None,  # /peers returns just this node's self-row
        telemetry_sink=resources.telemetry_sink,
        alert_dispatcher=None,  # alerts are coordinator-owned (mesh-fed)
        queue_manager=resources.queue_manager,
        chat_manager=resources.chat_manager,
    )
    return GatewayAssembly(service=service, resources=resources)


async def _run(config: TuringConfig) -> None:
    """Build, start, and run the standalone gateway until a signal arrives."""
    assembly = await build_assembly(config)
    await assembly.service.start()
    logger.info(
        "gateway_standalone_started",
        node=config.node_name,
        bind=config.gateway_bind,
        port=config.gateway_port,
        msg="standalone gateway running; projections are local until the IPC split (ADR 0010 §6)",
    )

    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("gateway_standalone_shutdown_requested")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await shutdown_event.wait()
    finally:
        logger.info("gateway_standalone_shutting_down")
        await assembly.service.stop()
        with contextlib.suppress(Exception):
            await assembly.resources.ring_buffer.close()
        logger.info("gateway_standalone_stopped")


def main() -> None:
    """CLI entry point for the ``turing-gateway`` command / ``python -m turing.gateway``."""
    config = TuringConfig()
    setup_logging(config)
    _check_token(config)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(config))
    sys.exit(0)


if __name__ == "__main__":
    main()
