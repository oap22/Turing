"""Tests for the core Agent loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from turing.agent.core import Agent
from turing.llm.base import LLMResponse, Role, ToolCall, ToolDefinition
from turing.memory.retriever import RetrievalResult
from turing.tools.base import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_config() -> MagicMock:
    config = MagicMock()
    config.node_name = "test-node"
    config.node_id = "test-id-001"
    config.env = "development"
    config.mesh_enabled = False
    config.learning_auto_extract = False
    config.learning_extract_interval = 5
    config.admin_user_ids = []
    return config


def _make_mock_memory_store() -> AsyncMock:
    store = AsyncMock()
    store.get_active_conversation.return_value = None
    store.create_conversation.return_value = "conv-001"
    store.add_message.return_value = 1
    store.touch_conversation.return_value = None
    return store


def _make_mock_retriever() -> AsyncMock:
    retriever = AsyncMock()
    retriever.retrieve.return_value = RetrievalResult(
        recent_messages=[],
        relevant_memories=[],
        user_preferences={},
        relevant_facts=[],
    )
    return retriever


def _make_mock_tool_registry() -> MagicMock:
    registry = MagicMock()
    registry.get_definitions.return_value = []
    registry.get_all.return_value = []
    registry.execute = AsyncMock(return_value=ToolResult(success=True, output="tool output"))
    return registry


def _make_mock_safety_gate() -> AsyncMock:
    from turing.agent.safety import SafetyCheckResult, SafetyDecision

    gate = AsyncMock()
    gate.check.return_value = SafetyCheckResult(
        decision=SafetyDecision.APPROVED,
        reason="Auto-approved",
        risk_level="low",
    )
    gate.log_action = AsyncMock()
    return gate


def _make_agent(
    config=None,
    llm_router=None,
    memory_store=None,
    memory_retriever=None,
    tool_registry=None,
    safety_gate=None,
) -> Agent:
    return Agent(
        config=config or _make_mock_config(),
        llm_router=llm_router or AsyncMock(),
        memory_store=memory_store or _make_mock_memory_store(),
        memory_retriever=memory_retriever or _make_mock_retriever(),
        tool_registry=tool_registry or _make_mock_tool_registry(),
        safety_gate=safety_gate or _make_mock_safety_gate(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_simple_message_returns_text():
    """When the LLM returns text without tool calls, the agent returns it."""
    llm_router = AsyncMock()
    llm_router.route.return_value = LLMResponse(
        content="Hello! How can I help you?",
        tool_calls=[],
        model="test-model",
    )

    agent = _make_agent(llm_router=llm_router)

    result = await agent.handle_message(
        message="Hello",
        channel_id="ch-1",
        user_id="user-1",
        user_name="TestUser",
    )

    assert result == "Hello! How can I help you?"
    llm_router.route.assert_awaited_once()


async def test_tool_use_loop():
    """LLM returns a tool call, tool executes, then LLM returns text."""
    tool_call = ToolCall(id="tc-1", name="shell", arguments={"command": "echo hi"})
    tool_registry = _make_mock_tool_registry()
    tool_registry.get_definitions.return_value = [
        ToolDefinition(
            name="shell",
            description="Run shell commands",
            parameters={"type": "object", "properties": {}},
        )
    ]

    llm_router = AsyncMock()
    # First call: return a tool call
    # Second call: return final text
    llm_router.route.side_effect = [
        LLMResponse(content="", tool_calls=[tool_call], model="test"),
        LLMResponse(content="The command output was 'tool output'.", tool_calls=[], model="test"),
    ]

    safety_gate = _make_mock_safety_gate()
    agent = _make_agent(
        llm_router=llm_router,
        tool_registry=tool_registry,
        safety_gate=safety_gate,
    )

    result = await agent.handle_message(
        message="Run echo hi",
        channel_id="ch-1",
        user_id="user-1",
        user_name="TestUser",
    )

    assert "tool output" in result
    assert llm_router.route.await_count == 2
    tool_registry.execute.assert_awaited_once_with("shell", command="echo hi")


async def test_parallel_tool_calls_append_one_assistant_turn():
    """A multi-tool turn must be recorded once, not once per tool call.

    The assistant message already carries every tool_call, so appending it
    inside the per-call loop duplicated it N times — and because each copy
    carries all N tool_use blocks, the history the LLM re-reads on the next
    iteration grew with the square of the tool-call count.
    """
    calls = [
        ToolCall(id=f"tc-{i}", name="shell", arguments={"command": f"echo {i}"}) for i in range(4)
    ]
    tool_registry = _make_mock_tool_registry()
    tool_registry.get_definitions.return_value = [
        ToolDefinition(
            name="shell",
            description="Run shell commands",
            parameters={"type": "object", "properties": {}},
        )
    ]

    llm_router = AsyncMock()
    llm_router.route.side_effect = [
        LLMResponse(content="", tool_calls=calls, model="test"),
        LLMResponse(content="All four ran.", tool_calls=[], model="test"),
    ]

    agent = _make_agent(
        llm_router=llm_router,
        tool_registry=tool_registry,
        safety_gate=_make_mock_safety_gate(),
    )

    await agent.handle_message(
        message="Run all four",
        channel_id="ch-1",
        user_id="user-1",
        user_name="TestUser",
    )

    # Inspect the history handed to the second LLM call.
    second_call_messages = llm_router.route.await_args_list[1].kwargs["messages"]
    assistant_turns = [m for m in second_call_messages if m.role == Role.ASSISTANT]
    tool_turns = [m for m in second_call_messages if m.role == Role.TOOL]

    assert len(assistant_turns) == 1, "the assistant turn must appear exactly once"
    assert assistant_turns[0].tool_calls == calls
    # One result per call, in call order, each tied to its own tool_call id.
    assert [m.tool_call_id for m in tool_turns] == [c.id for c in calls]


async def test_max_iterations_safety():
    """If LLM keeps returning tool calls, we stop at MAX_ITERATIONS."""
    tool_call = ToolCall(id="tc-loop", name="shell", arguments={"command": "echo loop"})
    tool_registry = _make_mock_tool_registry()
    tool_registry.get_definitions.return_value = [
        ToolDefinition(
            name="shell",
            description="Run shell commands",
            parameters={"type": "object", "properties": {}},
        )
    ]

    llm_router = AsyncMock()
    # Always return tool calls, never a text-only response
    llm_router.route.return_value = LLMResponse(content="", tool_calls=[tool_call], model="test")

    agent = _make_agent(llm_router=llm_router, tool_registry=tool_registry)

    result = await agent.handle_message(
        message="Loop forever",
        channel_id="ch-1",
        user_id="user-1",
        user_name="TestUser",
    )

    # Agent should stop and return something after MAX_ITERATIONS
    assert llm_router.route.await_count == Agent.MAX_ITERATIONS
    # It should return the fallback message since content was empty
    assert "unable to generate" in result.lower() or result == ""


async def test_safety_denied():
    """When safety gate denies a tool call, the agent reports the denial."""
    from turing.agent.safety import SafetyCheckResult, SafetyDecision

    tool_call = ToolCall(id="tc-denied", name="shell", arguments={"command": "rm -rf /"})
    tool_registry = _make_mock_tool_registry()
    tool_registry.get_definitions.return_value = [
        ToolDefinition(
            name="shell",
            description="Run shell commands",
            parameters={"type": "object", "properties": {}},
        )
    ]

    safety_gate = AsyncMock()
    safety_gate.check.return_value = SafetyCheckResult(
        decision=SafetyDecision.DENIED,
        reason="Command blocked by safety rule",
        risk_level="high",
    )
    safety_gate.log_action = AsyncMock()

    llm_router = AsyncMock()
    llm_router.route.side_effect = [
        LLMResponse(content="", tool_calls=[tool_call], model="test"),
        LLMResponse(
            content="I cannot run that command because it was blocked by safety rules.",
            tool_calls=[],
            model="test",
        ),
    ]

    agent = _make_agent(
        llm_router=llm_router,
        tool_registry=tool_registry,
        safety_gate=safety_gate,
    )

    result = await agent.handle_message(
        message="Delete everything",
        channel_id="ch-1",
        user_id="user-1",
        user_name="TestUser",
    )

    # Tool should NOT have been executed
    tool_registry.execute.assert_not_awaited()
    # Safety gate should have been checked
    safety_gate.check.assert_awaited_once()
    # Agent should report the denial
    assert "blocked" in result.lower() or "cannot" in result.lower() or "safety" in result.lower()


async def test_conversation_storage():
    """Verify that user and assistant messages are stored in memory."""
    memory_store = _make_mock_memory_store()
    llm_router = AsyncMock()
    llm_router.route.return_value = LLMResponse(
        content="Here is my response.", tool_calls=[], model="test"
    )

    agent = _make_agent(llm_router=llm_router, memory_store=memory_store)

    await agent.handle_message(
        message="Store this",
        channel_id="ch-1",
        user_id="user-1",
        user_name="TestUser",
    )

    # Conversation should be created
    memory_store.create_conversation.assert_awaited_once_with("ch-1")

    # Two add_message calls: one for user, one for assistant
    assert memory_store.add_message.await_count == 2
    calls = memory_store.add_message.await_args_list

    # First call: user message
    assert calls[0].args == ("conv-001", "user", "Store this", "user-1", "TestUser")

    # Second call: assistant message
    assert calls[1].args == ("conv-001", "assistant", "Here is my response.", "", "Turing")


async def test_existing_conversation_reuse():
    """If an active conversation exists, it should be reused, not recreated."""
    memory_store = _make_mock_memory_store()
    memory_store.get_active_conversation.return_value = {"id": "existing-conv"}

    llm_router = AsyncMock()
    llm_router.route.return_value = LLMResponse(content="Response", tool_calls=[], model="test")

    agent = _make_agent(llm_router=llm_router, memory_store=memory_store)

    await agent.handle_message(
        message="Hello again",
        channel_id="ch-1",
        user_id="user-1",
        user_name="TestUser",
    )

    # Should NOT create a new conversation
    memory_store.create_conversation.assert_not_awaited()
    # Messages should use the existing conversation ID. Storing the message is
    # also what bumps last_message_at, so no separate touch_conversation call
    # is made — add_message already stamps it.
    memory_store.touch_conversation.assert_not_awaited()
    calls = memory_store.add_message.await_args_list
    assert calls[0].args[0] == "existing-conv"


async def test_system_prompt_includes_node_info():
    """System prompt should include node name and ID."""
    agent = _make_agent()

    from turing.agent.context import AgentContext

    context = AgentContext(
        message="test",
        channel_id="ch-1",
        user_id="user-1",
        user_name="TestUser",
        node_id="test-id-001",
        node_name="test-node",
    )

    prompt = agent._build_system_prompt(context)
    assert "test-node" in prompt
    assert "test-id-001" in prompt


async def test_system_prompt_includes_preferences():
    """System prompt should include user preferences when available."""
    agent = _make_agent()

    from turing.agent.context import AgentContext

    context = AgentContext(
        message="test",
        channel_id="ch-1",
        user_id="user-1",
        user_name="TestUser",
        user_preferences={"language": "Python", "timezone": "UTC"},
        node_id="test-id",
        node_name="test-node",
    )

    prompt = agent._build_system_prompt(context)
    assert "language" in prompt
    assert "Python" in prompt
    assert "timezone" in prompt
