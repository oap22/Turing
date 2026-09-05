"""Turing -- entry point for ``python -m turing``."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from turing import oscompat
from turing.config import TuringConfig
from turing.logging import setup_logging

if TYPE_CHECKING:
    from turing.coordinator.alerts.dispatcher import AlertDispatcher
    from turing.gateway.ring_buffer import RingBuffer
    from turing.gateway.telemetry_sink import TelemetrySink

logger = structlog.get_logger("turing")


async def _run(config: TuringConfig) -> None:
    """Initialize all components and run the application."""

    # 1. Initialize Memory
    from turing.memory.embeddings import EmbeddingModel
    from turing.memory.retriever import MemoryRetriever
    from turing.memory.store import MemoryStore
    from turing.memory.vectors import VectorStore

    memory_store = MemoryStore(str(config.db_path))
    await memory_store.initialize()

    embedding_model = EmbeddingModel(config.embedding_model_path)
    await embedding_model.initialize()

    vector_store = VectorStore()
    await vector_store.initialize(memory_store.db)

    retriever = MemoryRetriever(memory_store, vector_store, embedding_model)

    # 2. Initialize LLM
    from turing.llm.classifier import ComplexityClassifier
    from turing.llm.cloud import ClaudeProvider
    from turing.llm.local import OllamaProvider
    from turing.llm.router import LLMRouter, warn_if_local_only_disables_tools

    cloud_provider = ClaudeProvider(api_key=config.anthropic_api_key, model=config.anthropic_model)
    local_provider = OllamaProvider(host=config.ollama_host, model=config.ollama_model)
    classifier = ComplexityClassifier()

    # Map config routing mode to router's expected values
    from typing import Literal, cast

    routing_mode_map = {"auto": "auto", "cloud": "cloud_only", "local": "local_only"}
    routing_mode = cast(
        "Literal['cloud_only', 'local_only', 'auto']",
        routing_mode_map.get(config.llm_routing_mode, "auto"),
    )
    llm_router = LLMRouter(
        cloud_provider,
        local_provider,
        classifier,
        routing_mode,
        local_tools_enabled=config.ollama_tools_enabled,
    )

    # 3. Initialize Tools
    from turing.tools.agent_mailbox import register_agent_mailbox_tool
    from turing.tools.base import ToolRegistry
    from turing.tools.filesystem import FileSystemTool
    from turing.tools.network import NetworkTool
    from turing.tools.process import ProcessTool
    from turing.tools.shell import ShellTool
    from turing.tools.system_info import SystemInfoTool

    tool_registry = ToolRegistry()
    tool_registry.register(ShellTool(config))
    tool_registry.register(FileSystemTool(config))
    tool_registry.register(ProcessTool())
    tool_registry.register(NetworkTool())
    tool_registry.register(SystemInfoTool())
    await register_agent_mailbox_tool(tool_registry, config)

    # 4. Load Plugins
    from turing.plugins.loader import PluginLoader
    from turing.plugins.registry import PluginRegistry

    plugin_registry = PluginRegistry()
    plugin_loader = PluginLoader(config)
    plugins_dir = Path(__file__).parent.parent.parent / "plugins"
    if plugins_dir.exists():
        for manifest in plugin_loader.scan_directory(plugins_dir):
            try:
                plugin = plugin_loader.load_plugin(manifest)
                await plugin.setup(config)
                plugin_registry.register(plugin)
                for tool in plugin.get_tools():
                    tool_registry.register(tool)
                logger.info("plugin.loaded", name=plugin.name)
            except Exception as e:
                logger.warning("plugin.load_failed", name=manifest.name, error=str(e))

    # Issue #158 — warn loudly when local_only is set with tools registered.
    warn_if_local_only_disables_tools(
        routing_mode,
        len(tool_registry.get_all()),
        local_tools_enabled=config.ollama_tools_enabled,
    )

    # 5. Safety Gate
    from turing.agent.safety import SafetyGate

    safety_gate = SafetyGate(config, audit_store=memory_store)

    # 6. Mesh (optional) — NATS-backed peer presence (ADR-0008), signed
    # end-to-end via SignedTransport (issue #348). Fail-closed: without a
    # signing seed AND a trusted-keys map, presence never starts and unsigned
    # presence traffic is never consumed.
    import time as _mesh_time

    from turing.mesh.node import MeshNode
    from turing.mesh.presence import PresenceService
    from turing.transport.nats_bus import NatsBus
    from turing.transport.signed_transport import SignedTransport
    from turing.transport.signer import MessageSigner

    mesh_node = None
    presence: PresenceService | None = None
    mesh_bus: NatsBus | None = None
    if config.mesh_enabled:
        mesh_node = MeshNode(config)
        mesh_node.capabilities = [t.name for t in tool_registry.get_all()]
        await mesh_node.start()
        if not config.mesh_signing_seed or not config.mesh_trusted_keys:
            logger.warning(
                "mesh.presence_signing_unconfigured",
                msg="TURING_MESH_SIGNING_SEED and/or TURING_MESH_TRUSTED_KEYS "
                "unset; presence requires signed transport (issue #348) and "
                "will not start — node will operate as a singleton",
            )
        else:
            try:
                signer = MessageSigner.from_seed_hex(config.mesh_signing_seed)
                trusted_keys = {
                    node_id: bytes.fromhex(key_hex)
                    for node_id, key_hex in config.mesh_trusted_keys.items()
                }
                mesh_bus = await NatsBus.connect(
                    url=config.nats_url,
                    tls_enabled=config.nats_tls_enabled,
                    nkey_seed=config.nats_nkey_seed,
                    lan_only=config.nats_lan_only,
                )
                presence_transport = SignedTransport(
                    bus=mesh_bus,
                    signer=signer,
                    trusted_keys=trusted_keys,
                    now_ms=lambda: int(_mesh_time.time() * 1000),
                )
                presence = PresenceService(mesh_node, presence_transport)
                await presence.start()
                logger.info("mesh.started", node=config.node_name)
            except Exception as exc:
                logger.warning(
                    "mesh.presence_unavailable",
                    error=str(exc),
                    msg="NATS bus unavailable or presence signing "
                    "misconfigured; node will operate as a singleton",
                )

    # 7. Agent
    from turing.agent.core import Agent
    from turing.agent.executor import Executor

    executor = Executor(tool_registry, safety_gate)
    agent = Agent(
        config=config,
        llm_router=llm_router,
        memory_store=memory_store,
        memory_retriever=retriever,
        tool_registry=tool_registry,
        safety_gate=safety_gate,
        mesh_node=mesh_node,
        plugin_registry=plugin_registry,
    )
    agent.executor = executor

    # 8. Hardware-safety alerts (PRD #228) — build the alert dispatcher and
    # wire it into mesh presence + the gateway. Per ADR-0010 §2 the closed-laptop
    # fallback transport is ntfy (self-hosted on the Surface coordinator),
    # replacing the retired Discord-DM fallback.
    alert_dispatcher: AlertDispatcher | None = None
    telemetry_sink: TelemetrySink | None = None
    alerts_ring_buffer: RingBuffer | None = None
    if config.mesh_enabled and presence is not None:
        from turing.coordinator.alerts.dispatcher import AlertDispatcher, ReachabilityClock
        from turing.coordinator.alerts.ntfy_client import NtfyAlertClient

        ntfy_client = NtfyAlertClient(config.coordinator_ntfy_base_url, config.operator_ntfy_topic)
        if config.operator_ntfy_topic is None or config.coordinator_ntfy_base_url is None:
            logger.warning(
                "alerts.ntfy_fallback_disabled",
                msg="TURING_OPERATOR_NTFY_TOPIC / TURING_COORDINATOR_NTFY_BASE_URL unset; "
                "hardware-safety alert pushes disabled",
            )
        reachability_clock: ReachabilityClock | None = None
        if config.gateway_enabled:
            from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig
            from turing.gateway.telemetry_sink import TelemetrySink

            # In-memory buffer — this sink serves purely as the gateway's WS
            # fan-out hub for alert frames; alert history is not persisted.
            alerts_ring_buffer = RingBuffer(
                RingBufferConfig(
                    path=Path(":memory:"),
                    retention_seconds=3600,
                    max_bytes=16 * 1024 * 1024,
                )
            )
            await alerts_ring_buffer.open()
            sink = TelemetrySink(buffer=alerts_ring_buffer)
            telemetry_sink = sink

            def _spa_last_send() -> int | None:
                return sink.last_send_ms

            reachability_clock = _spa_last_send
        alert_dispatcher = AlertDispatcher(
            ntfy_client=ntfy_client,
            reachability_clock=reachability_clock,
        )
        presence.set_alert_dispatcher(alert_dispatcher)

    # 8b. Operator UI gateway (pi-alpha only)
    gateway = None
    if config.gateway_enabled:
        import time as _time

        from turing.coordinator.episode_rewards import EpisodeRewardsStore
        from turing.gateway.chat_manager import ChatManager
        from turing.gateway.queue_manager import QueueManager
        from turing.gateway.service import GatewayService

        # The webui question-queue manager is the primary work-direction
        # surface (ADR 0010 §1) that replaces the retired Discord task bot. Its
        # curation decisions write to the shared episode-rewards store with
        # magnitudes identical to the Discord path. Frame fan-out rides the
        # telemetry sink, the same path alert frames use.
        rewards_store = EpisodeRewardsStore()
        _gateway_broadcast = telemetry_sink._broadcast if telemetry_sink is not None else None
        queue_manager = QueueManager(
            rewards=rewards_store,
            broadcast=_gateway_broadcast,
            now_ms=lambda: int(_time.time() * 1000),
        )
        # The chat pane is the secondary, ad-hoc surface (ADR 0010 §1, Slice E).
        # It REUSES Slice C's reward emitter: ChatManager shares the same
        # rewards_store and delegates per-subtask thumbs to a QueueManager, so a
        # chat thumb is byte-identical to a queue curation in episode_rewards.
        chat_manager = ChatManager(
            rewards=rewards_store,
            broadcast=_gateway_broadcast,
            now_ms=lambda: int(_time.time() * 1000),
        )

        gateway = GatewayService(
            token=config.gateway_token,
            bind=config.gateway_bind,
            port=config.gateway_port,
            node_name=config.node_name,
            mesh_node=mesh_node,
            telemetry_sink=telemetry_sink,
            alert_dispatcher=alert_dispatcher,
            queue_manager=queue_manager,
            chat_manager=chat_manager,
        )
        await gateway.start()

    logger.info("turing.starting", node=config.node_name, env=config.env)

    # 9. Run with graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("turing.shutdown_requested")
        shutdown_event.set()

    # add_signal_handler raises NotImplementedError on Windows (Proactor
    # loop); oscompat falls back to signal.signal so startup survives.
    loop = asyncio.get_running_loop()
    oscompat.install_signal_handlers(loop, _signal_handler)

    try:
        # The coordinator/gateway/mesh run as long-lived background services
        # (ADR 0010 §6). With the Discord task bot retired there is no
        # foreground client to await — the process stays up until a signal
        # sets the shutdown event.
        await shutdown_event.wait()
    finally:
        # Cleanup
        logger.info("turing.shutting_down")
        if gateway:
            await gateway.stop()
        if presence:
            await presence.stop()
        if alerts_ring_buffer is not None:
            with contextlib.suppress(Exception):
                await alerts_ring_buffer.close()
        if mesh_bus:
            with contextlib.suppress(Exception):
                await mesh_bus.close()
        if mesh_node:
            await mesh_node.stop()
        await memory_store.close()
        logger.info("turing.stopped")


def main() -> None:
    """CLI entry point for the ``turing`` command."""
    config = TuringConfig()
    setup_logging(config)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(config))
    sys.exit(0)


if __name__ == "__main__":
    main()
