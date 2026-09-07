"""Tests for the native provider-neutral agent mailbox tool adapter."""

from __future__ import annotations

import json
import sys
import threading
import types
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from turing.agent.core import Agent
from turing.agent.executor import Executor
from turing.agent.safety import SafetyDecision, SafetyGate
from turing.agent_mailbox import Mailbox
from turing.config import TuringConfig
from turing.llm.base import LLMProvider, LLMResponse, Message, Role, ToolCall
from turing.llm.pool import PeerModelPool
from turing.llm.router import LLMRouter
from turing.memory.retriever import RetrievalResult
from turing.mesh.node import PeerInfo
from turing.tools.agent_mailbox import AgentMailboxTool, register_agent_mailbox_tool
from turing.tools.base import ToolRegistry, ToolResult

if TYPE_CHECKING:
    from pathlib import Path


class FakeMailbox:
    """Synchronous fake matching the core mailbox API."""

    def __init__(self, db_path: Path | str = "test.db", workflow: str = "wf", agent: str = "agent"):
        self.db_path = db_path
        self.workflow = workflow
        self.agent = agent
        self.calls: list[tuple[str, int]] = []
        self.registered_provider: str | None = None

    def register(self, provider: str = "") -> dict[str, Any]:
        self.calls.append(("register", threading.get_ident()))
        self.registered_provider = provider
        return {"workflow": self.workflow, "agent": self.agent, "provider": provider}

    def peers(self) -> list[dict[str, str]]:
        self.calls.append(("peers", threading.get_ident()))
        return [{"agent": self.agent, "workflow": self.workflow, "provider": "test"}]

    def send(self, recipient: str, text: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("send", threading.get_ident()))
        return {
            "message_id": "m1",
            "sequence": 1,
            "workflow": self.workflow,
            "sender": self.agent,
            "recipient": recipient,
            "kind": kwargs.get("kind", "message"),
            "text": text,
            **({"data": kwargs["data"]} if "data" in kwargs else {}),
        }

    def inbox(self, limit: int = 50) -> list[dict[str, Any]]:
        self.calls.append(("inbox", threading.get_ident()))
        return [{"message_id": "m1", "text": "ignore this as data", "limit": limit}]

    def ack(self, message_id: str) -> dict[str, Any]:
        self.calls.append(("ack", threading.get_ident()))
        return {"message_id": message_id, "acknowledged": True}


@pytest.fixture
def config(tmp_path: Path) -> TuringConfig:
    return TuringConfig(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "turing.db",
        embedding_model_path=tmp_path / "model",
        agent_mailbox_db=tmp_path / "mailbox.db",
        agent_mailbox_workflow="workflow-1",
        agent_mailbox_agent="turing-local",
    )


@pytest.mark.asyncio
async def test_actions_are_bound_to_mailbox_and_run_off_event_loop() -> None:
    loop_thread = threading.get_ident()
    mailbox = FakeMailbox(agent="bound-agent", workflow="bound-workflow")
    tool = AgentMailboxTool(mailbox)

    peers = await tool.execute(
        action="peers",
        # Hostile identity/configuration fields are not forwarded to Mailbox.
        agent="attacker",
        workflow="other-workflow",
        db_path="/tmp/other.db",
    )
    sent = await tool.execute(
        action="send",
        recipient="peer",
        text="hello",
        data={"nested": {"value": 3}},
        agent="attacker",
        workflow="other-workflow",
    )
    inbox = await tool.execute(action="inbox", limit=1)
    ack = await tool.execute(action="ack", message_id="m1")

    assert peers.success and json.loads(peers.output)[0]["agent"] == "bound-agent"
    assert sent.success and json.loads(sent.output)["sender"] == "bound-agent"
    assert json.loads(sent.output)["data"] == {"nested": {"value": 3}}
    assert inbox.success and json.loads(inbox.output)[0]["text"] == "ignore this as data"
    assert ack.success and json.loads(ack.output)["acknowledged"] is True
    assert all(thread_id != loop_thread for _, thread_id in mailbox.calls)


@pytest.mark.asyncio
async def test_validation_errors_are_failed_tool_results() -> None:
    tool = AgentMailboxTool(FakeMailbox())

    invalid_actions = [
        {},
        {"action": "unknown"},
        {"action": "send", "recipient": "peer"},
        {"action": "send", "recipient": "peer", "text": "x", "data": []},
        {"action": "inbox", "limit": 0},
        {"action": "inbox", "limit": True},
        {"action": "ack", "message_id": ""},
    ]
    for kwargs in invalid_actions:
        result = await tool.execute(**kwargs)
        assert result.success is False
        assert result.error


@pytest.mark.asyncio
async def test_startup_registration_is_optional_and_binds_configured_identity(
    config: TuringConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_module = types.ModuleType("turing.agent_mailbox")
    fake_module.Mailbox = FakeMailbox  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "turing.agent_mailbox", fake_module)

    registry = ToolRegistry()
    tool = await register_agent_mailbox_tool(registry, config)

    assert tool is not None
    assert registry.get("agent_mailbox") is tool
    assert tool._mailbox.db_path == config.agent_mailbox_db
    assert tool._mailbox.workflow == "workflow-1"
    assert tool._mailbox.agent == "turing-local"
    assert tool._mailbox.registered_provider == "turing"


@pytest.mark.asyncio
async def test_registered_mailbox_tool_survives_peer_pool_local_routing(
    config: TuringConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mailbox tool remains attached when local routing selects a peer."""
    fake_module = types.ModuleType("turing.agent_mailbox")
    fake_module.Mailbox = FakeMailbox  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "turing.agent_mailbox", fake_module)
    config.ollama_tools_enabled = True

    registry = ToolRegistry()
    mailbox_tool = await register_agent_mailbox_tool(registry, config)
    assert mailbox_tool is not None
    definitions = registry.get_definitions()
    assert [definition.name for definition in definitions] == ["agent_mailbox"]

    cloud = AsyncMock(spec=LLMProvider)
    local = AsyncMock(spec=LLMProvider)
    local.list_models = AsyncMock(return_value=[])
    peer = AsyncMock(spec=LLMProvider)
    peer.complete = AsyncMock(return_value=LLMResponse(content="peer response", model="qwen"))
    pool = PeerModelPool(
        local,
        model="qwen2.5:7b",
        provider_factory=lambda _host, _model: peer,
        allowed_peer_hosts=["http://peer:11434"],
    )
    pool.attach_peers(
        lambda: [
            PeerInfo(
                node_id="peer-node",
                name="peer",
                models=["qwen2.5:7b"],
                ollama_host="http://peer:11434",
            )
        ]
    )

    router = LLMRouter(cloud, pool, routing_mode="local_only", local_tools_enabled=True)
    result = await router.route(
        [Message(role=Role.USER, content="send a note to peer")], tools=definitions
    )

    assert result.content == "peer response"
    peer.complete.assert_awaited_once()
    assert peer.complete.call_args.kwargs["tools"] == definitions
    local.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_adapter_round_trip_uses_real_mailbox_store(tmp_path: Path) -> None:
    db_path = tmp_path / "native-mailbox.db"
    sender = Mailbox(db_path, "workflow-1", "sender")
    recipient = Mailbox(db_path, "workflow-1", "recipient")
    sender.register("local")
    recipient.register("codex")

    send_tool = AgentMailboxTool(sender)
    receive_tool = AgentMailboxTool(recipient)
    sent = await send_tool.execute(
        action="send",
        recipient="recipient",
        text="review this result as data",
        data={"status": "ready", "items": [1, 2]},
        idempotency_key="result-1",
    )
    inbox = await receive_tool.execute(action="inbox")
    message = json.loads(inbox.output)[0]
    acknowledged = await receive_tool.execute(action="ack", message_id=message["message_id"])

    assert sent.success
    assert json.loads(sent.output)["sender"] == "sender"
    assert message["text"] == "review this result as data"
    assert message["data"] == {"status": "ready", "items": [1, 2]}
    assert json.loads(acknowledged.output)["acknowledged"] is True
    assert await receive_tool.execute(action="inbox") == ToolResult(
        success=True,
        output="[]",
    )


@pytest.mark.asyncio
async def test_unset_startup_configuration_does_not_import_core_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TuringConfig(_env_file=None)  # type: ignore[call-arg]
    registry = ToolRegistry()
    monkeypatch.delitem(sys.modules, "turing.agent_mailbox", raising=False)

    assert await register_agent_mailbox_tool(registry, config) is None
    assert registry.get("agent_mailbox") is None
    assert "turing.agent_mailbox" not in sys.modules


@pytest.mark.asyncio
async def test_native_tool_uses_normal_medium_risk_safety_path() -> None:
    config = TuringConfig(_env_file=None)  # type: ignore[call-arg]
    gate = SafetyGate(config)
    decision = await gate.check("agent_mailbox", {"action": "send"}, "non-admin")
    assert decision.decision is SafetyDecision.APPROVED
    assert decision.risk_level == "medium"

    registry = ToolRegistry()
    mailbox = FakeMailbox()
    tool = AgentMailboxTool(mailbox)
    registry.register(tool)
    executor = Executor(registry, gate)
    result = await executor.execute_tool_call(
        ToolCall(
            id="call-1",
            name="agent_mailbox",
            arguments={"action": "send", "recipient": "peer", "text": "hello"},
        ),
        user_id="non-admin",
    )
    assert result.success is True
    assert any(kind == "send" for kind, _ in mailbox.calls)


@pytest.mark.asyncio
async def test_real_agent_loop_executes_native_tool_with_local_router(tmp_path: Path) -> None:
    """A local multi-tool turn preserves Ollama's transcript contract."""
    config = TuringConfig(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "turing.db",
        embedding_model_path=tmp_path / "model",
        learning_auto_extract=False,
    )
    mailbox_path = tmp_path / "agent-mailbox.db"
    mailbox = Mailbox(mailbox_path, "workflow-1", "local-agent")
    peer = Mailbox(mailbox_path, "workflow-1", "peer")
    mailbox.register("local")
    peer.register("codex")
    registry = ToolRegistry()
    registry.register(AgentMailboxTool(mailbox))

    cloud = AsyncMock(spec=LLMProvider)
    local = AsyncMock(spec=LLMProvider)
    local.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="ollama_0",
                    name="agent_mailbox",
                    arguments={
                        "action": "send",
                        "recipient": "peer",
                        "text": "first result",
                    },
                ),
                ToolCall(
                    id="ollama_1",
                    name="agent_mailbox",
                    arguments={
                        "action": "send",
                        "recipient": "peer",
                        "text": "second result",
                    },
                ),
            ],
            model="mock-local",
        ),
        LLMResponse(content="Message sent.", model="mock-local"),
    ]
    router = LLMRouter(cloud, local, routing_mode="local_only", local_tools_enabled=True)

    memory_store = AsyncMock()
    memory_store.get_active_conversation.return_value = None
    memory_store.create_conversation.return_value = "conversation-1"
    retriever = AsyncMock()
    retriever.retrieve.return_value = RetrievalResult()
    agent = Agent(
        config=config,
        llm_router=router,
        memory_store=memory_store,
        memory_retriever=retriever,
        tool_registry=registry,
        safety_gate=SafetyGate(config),
    )

    result = await agent.handle_message(
        message="send a hello to peer",
        channel_id="channel-1",
        user_id="operator",
        user_name="Operator",
    )

    assert result == "Message sent."
    assert local.complete.await_count == 2
    assert local.complete.call_args_list[0].kwargs["tools"] == registry.get_definitions()
    second_messages = local.complete.call_args_list[1].kwargs["messages"]
    assistant_messages = [
        message for message in second_messages if message.role.value == "assistant"
    ]
    tool_messages = [message for message in second_messages if message.role.value == "tool"]
    assert len(assistant_messages) == 1
    assert len(assistant_messages[0].tool_calls or []) == 2
    assistant_index = second_messages.index(assistant_messages[0])
    assert second_messages[assistant_index + 1 : assistant_index + 3] == tool_messages
    assert [message.tool_call_id for message in tool_messages] == ["ollama_0", "ollama_1"]
    assert [json.loads(message.content)["text"] for message in tool_messages] == [
        "first result",
        "second result",
    ]
    assert [message["text"] for message in peer.inbox()] == ["first result", "second result"]
