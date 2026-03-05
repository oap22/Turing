"""Agent context assembly — gathers all relevant information for decision-making."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from turing.config import TuringConfig
    from turing.memory.retriever import MemoryRetriever
    from turing.mesh.node import MeshNode

logger = structlog.get_logger("turing.agent.context")


@dataclass
class AgentContext:
    """All context for the agent to make decisions."""

    message: str
    channel_id: str
    user_id: str
    user_name: str
    history: list[dict[str, Any]] = field(default_factory=list)
    relevant_memories: list[dict[str, Any]] = field(default_factory=list)
    user_preferences: dict[str, str] = field(default_factory=dict)
    relevant_facts: list[dict[str, Any]] = field(default_factory=list)
    system_state: dict[str, Any] = field(default_factory=dict)
    node_id: str = ""
    node_name: str = ""
    peer_count: int = 0


async def build_context(
    message: str,
    channel_id: str,
    user_id: str,
    user_name: str,
    retriever: MemoryRetriever,
    config: TuringConfig,
    mesh_node: MeshNode | None = None,
) -> AgentContext:
    """Assemble full context from all sources.

    Retrieves memory (recent messages, semantic matches, user preferences,
    relevant facts) via the retriever and adds system state information.
    """
    # Retrieve all memory context in parallel via the retriever
    try:
        retrieval = await retriever.retrieve(
            message=message,
            channel_id=channel_id,
            user_id=user_id,
            limit=10,
        )
    except Exception as exc:
        logger.warning("context_retrieval_failed", error=str(exc))
        from turing.memory.retriever import RetrievalResult

        retrieval = RetrievalResult()

    # Build system state
    system_state: dict[str, Any] = {
        "node_name": config.node_name,
        "node_id": config.node_id,
        "env": config.env,
        "mesh_enabled": config.mesh_enabled,
    }

    # Add mesh information if available
    node_id = config.node_id
    node_name = config.node_name
    peer_count = 0

    if mesh_node is not None:
        node_id = mesh_node.node_id
        node_name = mesh_node.node_name
        peers = mesh_node.peers
        peer_count = len(peers)
        system_state["peers"] = [
            {"name": p.name, "node_id": p.node_id, "capabilities": p.capabilities}
            for p in peers.values()
        ]

    logger.debug(
        "context_built",
        channel_id=channel_id,
        user_id=user_id,
        history_count=len(retrieval.recent_messages),
        memory_count=len(retrieval.relevant_memories),
        fact_count=len(retrieval.relevant_facts),
        pref_count=len(retrieval.user_preferences),
        peer_count=peer_count,
    )

    return AgentContext(
        message=message,
        channel_id=channel_id,
        user_id=user_id,
        user_name=user_name,
        history=retrieval.recent_messages,
        relevant_memories=retrieval.relevant_memories,
        user_preferences=retrieval.user_preferences,
        relevant_facts=retrieval.relevant_facts,
        system_state=system_state,
        node_id=node_id,
        node_name=node_name,
        peer_count=peer_count,
    )
