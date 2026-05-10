"""Turing -- entry point for ``python -m turing``."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from pathlib import Path

import structlog

from turing.config import TuringConfig
from turing.logging import setup_logging

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
    routing_mode_map = {"auto": "auto", "cloud": "cloud_only", "local": "local_only"}
    routing_mode = routing_mode_map.get(config.llm_routing_mode, "auto")
    llm_router = LLMRouter(cloud_provider, local_provider, classifier, routing_mode)

    # 3. Initialize Tools
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
    warn_if_local_only_disables_tools(routing_mode, len(tool_registry.get_all()))

    # 5. Safety Gate
    from turing.agent.safety import SafetyGate

    safety_gate = SafetyGate(config, audit_store=memory_store)

    # 6. Mesh (optional)
    from turing.mesh.discovery import PeerDiscovery
    from turing.mesh.node import MeshNode

    mesh_node = None
    discovery = None
    if config.mesh_enabled:
        mesh_node = MeshNode(config)
        mesh_node.capabilities = [t.name for t in tool_registry.get_all()]
        await mesh_node.start()
        discovery = PeerDiscovery(mesh_node, config)
        await discovery.start()
        logger.info("mesh.started", node=config.node_name)

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

    # 8. Discord Bot
    from turing.discord_bot.bot import TuringBot

    bot = TuringBot(config, agent=agent, mesh_node=mesh_node, memory_store=memory_store)
    # Give executor reference to bot for confirmation views
    executor.bot = bot

    # 8b. Operator UI gateway (pi-alpha only)
    gateway = None
    if config.gateway_enabled:
        from turing.gateway.service import GatewayService

        gateway = GatewayService(
            token=config.gateway_token,
            bind=config.gateway_bind,
            port=config.gateway_port,
            node_name=config.node_name,
        )
        await gateway.start()

    logger.info("turing.starting", node=config.node_name, env=config.env)

    # 9. Run with graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("turing.shutdown_requested")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        bot_task = asyncio.create_task(bot.start_bot())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        _done, pending = await asyncio.wait(
            [bot_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
    finally:
        # Cleanup
        logger.info("turing.shutting_down")
        if gateway:
            await gateway.stop()
        if discovery:
            await discovery.stop()
        if mesh_node:
            await mesh_node.stop()
        await memory_store.close()
        if not bot.is_closed():
            await bot.close()
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
