"""End-to-end integration tests for the agent pipeline.

Uses real MemoryStore (in-memory SQLite) and real ToolRegistry with
mocked LLM responses and mocked subprocess calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from turing.agent.core import Agent
from turing.agent.safety import SafetyGate
from turing.llm.base import LLMResponse, Message, Role, ToolCall, ToolDefinition
from turing.memory.embeddings import EmbeddingModel
from turing.memory.retriever import MemoryRetriever
from turing.memory.store import MemoryStore
from turing.memory.vectors import VectorStore
from turing.tools.base import ToolRegistry, ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> MagicMock:
    config = MagicMock()
    config.node_name = "integration-test"
    config.node_id = "int-test-001"
    config.env = "development"
    config.mesh_enabled = False
    config.learning_auto_extract = False
    config.learning_extract_interval = 100
    config.discord_admin_ids = [42]
    config.sandbox_enabled = False
    config.sandbox_timeout = 30
    config.allowed_write_paths = ["/tmp"]
    return config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def memory_store():
    """Create a real in-memory MemoryStore."""
    store = MemoryStore(":memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture()
def embedding_model():
    """Create an EmbeddingModel that is not ready (returns zero vectors)."""
    model = EmbeddingModel(None)
    return model


@pytest.fixture()
async def vector_store(memory_store):
    """Create a VectorStore (may not have sqlite-vec extension)."""
    vs = VectorStore()
    try:
        await vs.initialize(memory_store.db)
    except Exception:
        # sqlite-vec may not be available in test environments
        pass
    return vs


@pytest.fixture()
def retriever(memory_store, vector_store, embedding_model):
    """Create a real MemoryRetriever."""
    return MemoryRetriever(memory_store, vector_store, embedding_model)


@pytest.fixture()
def tool_registry():
    """Create a real ToolRegistry with a mock tool."""

    class MockShellTool:
        """A mock shell tool for testing."""

        name = "shell"
        description = "Run shell commands"
        parameters = {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run"},
            },
            "required": ["command"],
        }
        risk_level = MagicMock()
        risk_level.value = "medium"
        requires_confirmation = False

        def to_tool_definition(self):
            return ToolDefinition(
                name=self.name,
                description=self.description,
                parameters=self.parameters,
            )

        async def execute(self, **kwargs):
            command = kwargs.get("command", "")
            return ToolResult(
                success=True,
                output=f"Mock output for: {command}",
            )

    registry = ToolRegistry()
    # We need to register a tool that implements the Tool interface
    # Use a real-ish mock
    from turing.tools.base import RiskLevel, Tool

    class TestShellTool(Tool):
        @property
        def name(self) -> str:
            return "shell"

        @property
        def description(self) -> str:
            return "Run shell commands"

        @property
        def parameters(self) -> dict:
            return {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to run"},
                },
                "required": ["command"],
            }

        @property
        def risk_level(self) -> RiskLevel:
            return RiskLevel.MEDIUM

        async def execute(self, **kwargs) -> ToolResult:
            command = kwargs.get("command", "")
            return ToolResult(success=True, output=f"Output: {command}")

    class TestSystemInfoTool(Tool):
        @property
        def name(self) -> str:
            return "system_info"

        @property
        def description(self) -> str:
            return "Get system information"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs) -> ToolResult:
            return ToolResult(
                success=True,
                output="CPU: 25%, Memory: 512MB/4GB",
            )

    registry.register(TestShellTool())
    registry.register(TestSystemInfoTool())
    return registry


@pytest.fixture()
def safety_gate():
    """Create a real SafetyGate."""
    config = _make_config()
    return SafetyGate(config)


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


async def test_full_pipeline_simple_message(memory_store, retriever, tool_registry, safety_gate):
    """End-to-end: send a simple message, verify response and storage."""
    config = _make_config()

    llm_router = AsyncMock()
    llm_router.route.return_value = LLMResponse(
        content="Hello! I am Turing, your AI assistant.",
        tool_calls=[],
        model="test-model",
    )

    agent = Agent(
        config=config,
        llm_router=llm_router,
        memory_store=memory_store,
        memory_retriever=retriever,
        tool_registry=tool_registry,
        safety_gate=safety_gate,
    )

    result = await agent.handle_message(
        message="Hello Turing!",
        channel_id="test-channel-1",
        user_id="user-42",
        user_name="Alice",
    )

    # Verify response
    assert result == "Hello! I am Turing, your AI assistant."

    # Verify LLM was called
    llm_router.route.assert_awaited_once()

    # Verify messages were stored in the real memory store
    messages = await memory_store.get_recent_messages("test-channel-1", limit=10)
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello Turing!"
    assert messages[0]["user_name"] == "Alice"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Hello! I am Turing, your AI assistant."


async def test_tool_use_pipeline(memory_store, retriever, tool_registry, safety_gate):
    """End-to-end: LLM requests a tool, tool executes, result fed back."""
    config = _make_config()

    tool_call = ToolCall(
        id="tc-integration",
        name="system_info",
        arguments={},
    )

    llm_router = AsyncMock()
    llm_router.route.side_effect = [
        # First call: LLM wants to use a tool
        LLMResponse(
            content="Let me check the system info.",
            tool_calls=[tool_call],
            model="test",
        ),
        # Second call: LLM responds with the tool result
        LLMResponse(
            content="Your system has CPU at 25% and 512MB/4GB memory.",
            tool_calls=[],
            model="test",
        ),
    ]

    agent = Agent(
        config=config,
        llm_router=llm_router,
        memory_store=memory_store,
        memory_retriever=retriever,
        tool_registry=tool_registry,
        safety_gate=safety_gate,
    )

    result = await agent.handle_message(
        message="How is my system doing?",
        channel_id="test-channel-2",
        user_id="user-42",
        user_name="Alice",
    )

    # Verify the final response includes tool results
    assert "25%" in result
    assert "512MB" in result

    # Verify LLM was called twice (once for tool call, once for final response)
    assert llm_router.route.await_count == 2

    # Verify the second LLM call included the tool result in messages
    second_call_args = llm_router.route.await_args_list[1]
    second_messages = second_call_args.kwargs.get("messages") or second_call_args.args[0]
    # Should contain the tool result somewhere in the messages
    tool_messages = [m for m in second_messages if m.role == Role.TOOL]
    assert len(tool_messages) == 1
    assert "CPU: 25%" in tool_messages[0].content


async def test_memory_persistence_across_messages(
    memory_store, retriever, tool_registry, safety_gate
):
    """Verify that messages accumulate in memory across interactions."""
    config = _make_config()

    call_count = 0

    async def mock_route(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return LLMResponse(
            content=f"Response {call_count}",
            tool_calls=[],
            model="test",
        )

    llm_router = AsyncMock()
    llm_router.route.side_effect = mock_route

    agent = Agent(
        config=config,
        llm_router=llm_router,
        memory_store=memory_store,
        memory_retriever=retriever,
        tool_registry=tool_registry,
        safety_gate=safety_gate,
    )

    # Send first message
    r1 = await agent.handle_message(
        message="First message",
        channel_id="persist-channel",
        user_id="user-1",
        user_name="Bob",
    )
    assert r1 == "Response 1"

    # Send second message
    r2 = await agent.handle_message(
        message="Second message",
        channel_id="persist-channel",
        user_id="user-1",
        user_name="Bob",
    )
    assert r2 == "Response 2"

    # Verify all 4 messages are in memory (2 user + 2 assistant)
    messages = await memory_store.get_recent_messages("persist-channel", limit=20)
    assert len(messages) == 4
    assert messages[0]["content"] == "First message"
    assert messages[1]["content"] == "Response 1"
    assert messages[2]["content"] == "Second message"
    assert messages[3]["content"] == "Response 2"


async def test_context_includes_history(memory_store, retriever, tool_registry, safety_gate):
    """On a second message, context should include previous messages."""
    config = _make_config()

    messages_sent_to_llm: list[list[Message]] = []

    async def capture_route(*args, **kwargs):
        msgs = kwargs.get("messages") or args[0]
        messages_sent_to_llm.append(list(msgs))
        return LLMResponse(content="Got it.", tool_calls=[], model="test")

    llm_router = AsyncMock()
    llm_router.route.side_effect = capture_route

    agent = Agent(
        config=config,
        llm_router=llm_router,
        memory_store=memory_store,
        memory_retriever=retriever,
        tool_registry=tool_registry,
        safety_gate=safety_gate,
    )

    # First message
    await agent.handle_message(
        message="My name is Alice",
        channel_id="context-channel",
        user_id="user-1",
        user_name="Alice",
    )

    # Second message — should have history in context
    await agent.handle_message(
        message="What is my name?",
        channel_id="context-channel",
        user_id="user-1",
        user_name="Alice",
    )

    # The second LLM call should include the first exchange in history
    second_call_messages = messages_sent_to_llm[1]
    # There should be at least the history messages plus the current one
    # History: user "My name is Alice", assistant "Got it."
    # Current: user "What is my name?"
    user_messages = [m for m in second_call_messages if m.role == Role.USER]
    assert len(user_messages) >= 2
    contents = [m.content for m in user_messages]
    assert "My name is Alice" in contents
    assert "What is my name?" in contents
