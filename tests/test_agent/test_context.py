"""Tests for :func:`turing.agent.build_context`.

Covers the three branches that distinguish the assembled context: the happy
path (retriever returns data, no mesh), the retrieval-failure fallback (the
retriever raises → empty ``RetrievalResult``), and the mesh-enabled path where
node identity + peers come from the live :class:`MeshNode`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from turing.agent.context import AgentContext, build_context
from turing.memory.retriever import RetrievalResult


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        node_name="pi-alpha",
        node_id="node-1",
        env="development",
        mesh_enabled=False,
    )


def _retriever(result: RetrievalResult) -> Any:
    r = SimpleNamespace()
    r.retrieve = AsyncMock(return_value=result)
    return r


@pytest.mark.asyncio
async def test_happy_path_populates_context_from_retrieval() -> None:
    retrieval = RetrievalResult(
        recent_messages=[{"role": "user", "content": "hi"}],
        relevant_memories=[{"text": "m"}],
        user_preferences={"tone": "terse"},
        relevant_facts=[{"fact": "f"}],
    )
    ctx = await build_context(
        message="hello",
        channel_id="c1",
        user_id="u1",
        user_name="Owen",
        retriever=_retriever(retrieval),
        config=_config(),
    )

    assert isinstance(ctx, AgentContext)
    assert ctx.history == [{"role": "user", "content": "hi"}]
    assert ctx.user_preferences == {"tone": "terse"}
    assert ctx.relevant_facts == [{"fact": "f"}]
    # No mesh node → identity falls back to config, no peers.
    assert ctx.node_id == "node-1"
    assert ctx.node_name == "pi-alpha"
    assert ctx.peer_count == 0
    assert ctx.system_state["mesh_enabled"] is False
    assert "peers" not in ctx.system_state


@pytest.mark.asyncio
async def test_retrieval_failure_falls_back_to_empty_result() -> None:
    retriever = SimpleNamespace()
    retriever.retrieve = AsyncMock(side_effect=RuntimeError("db down"))

    ctx = await build_context(
        message="hello",
        channel_id="c1",
        user_id="u1",
        user_name="Owen",
        retriever=retriever,
        config=_config(),
    )

    # Fallback context is empty but well-formed — the agent still runs.
    assert ctx.history == []
    assert ctx.relevant_memories == []
    assert ctx.user_preferences == {}
    assert ctx.relevant_facts == []


@pytest.mark.asyncio
async def test_mesh_node_supplies_identity_and_peers() -> None:
    peers = {
        "node-2": SimpleNamespace(
            node_id="node-2", name="pi-beta", capabilities=["shell", "search"]
        ),
        "node-3": SimpleNamespace(node_id="node-3", name="pi-gamma", capabilities=[]),
    }
    mesh_node = SimpleNamespace(node_id="node-1", node_name="pi-alpha", peers=peers)

    ctx = await build_context(
        message="hello",
        channel_id="c1",
        user_id="u1",
        user_name="Owen",
        retriever=_retriever(RetrievalResult()),
        config=_config(),
        mesh_node=mesh_node,
    )

    assert ctx.node_id == "node-1"
    assert ctx.node_name == "pi-alpha"
    assert ctx.peer_count == 2
    peer_names = {p["name"] for p in ctx.system_state["peers"]}
    assert peer_names == {"pi-beta", "pi-gamma"}
    beta = next(p for p in ctx.system_state["peers"] if p["name"] == "pi-beta")
    assert beta["capabilities"] == ["shell", "search"]
