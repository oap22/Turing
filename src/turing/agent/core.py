"""Core autonomous agent implementing the perceive-think-act-remember loop."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from turing.agent.context import AgentContext, build_context
from turing.agent.executor import Executor
from turing.llm.base import Message, Role
from turing.telemetry import traced

if TYPE_CHECKING:
    from turing.agent.safety import SafetyGate
    from turing.config import TuringConfig
    from turing.llm.router import LLMRouter
    from turing.memory.retriever import MemoryRetriever
    from turing.memory.store import MemoryStore
    from turing.mesh.node import MeshNode
    from turing.plugins.registry import PluginRegistry
    from turing.tools.base import ToolRegistry

logger = structlog.get_logger("turing.agent.core")

_BASE_SYSTEM_PROMPT = """\
You are Turing, an autonomous AI assistant running on a Raspberry Pi network.
You help users by answering questions, running tools, and managing systems.

Core principles:
- Be helpful, accurate, and concise.
- Use tools when a task requires system interaction; otherwise respond directly.
- Always explain what you did when using tools.
- If you are unsure, say so rather than guessing.
- Respect safety boundaries — never attempt to bypass security controls.

When using tools:
- Choose the most appropriate tool for the task.
- Provide clear, well-formed arguments.
- Report tool results clearly, including any errors.
"""


class Agent:
    """Core autonomous agent implementing perceive-think-act-remember.

    Processes user messages through a loop of LLM reasoning and tool
    execution, storing all interactions in persistent memory.
    """

    MAX_ITERATIONS = 10

    def __init__(
        self,
        config: TuringConfig,
        llm_router: LLMRouter,
        memory_store: MemoryStore,
        memory_retriever: MemoryRetriever,
        tool_registry: ToolRegistry,
        safety_gate: SafetyGate,
        mesh_node: MeshNode | None = None,
        plugin_registry: PluginRegistry | None = None,
    ) -> None:
        self.config = config
        self.llm_router = llm_router
        self.memory_store = memory_store
        self.memory_retriever = memory_retriever
        self.tool_registry = tool_registry
        self.safety_gate = safety_gate
        self.mesh_node = mesh_node
        self.plugin_registry = plugin_registry
        self.executor = Executor(tool_registry, safety_gate)
        self._interaction_count = 0
        self._background_tasks: set[asyncio.Task] = set()

    def _build_system_prompt(self, context: AgentContext) -> str:
        """Build a dynamic system prompt incorporating all context sources."""
        parts: list[str] = [_BASE_SYSTEM_PROMPT]

        # Node identity
        parts.append(f"\nYou are running on node '{context.node_name}' (ID: {context.node_id}).")

        # User preferences
        if context.user_preferences:
            pref_lines = [f"- {k}: {v}" for k, v in context.user_preferences.items()]
            parts.append("\nKnown preferences for this user:\n" + "\n".join(pref_lines))

        # Available tools
        tools = self.tool_registry.get_all()
        if tools:
            tool_names = [t.name for t in tools]
            parts.append(f"\nAvailable tools: {', '.join(tool_names)}")

        # Mesh peers
        if context.peer_count > 0:
            peer_info = context.system_state.get("peers", [])
            peer_names = [p.get("name", "unknown") for p in peer_info]
            parts.append(
                f"\nMesh network: {context.peer_count} peer(s) connected: {', '.join(peer_names)}"
            )

        # Plugin additions
        if self.plugin_registry is not None:
            additions = self.plugin_registry.get_system_prompt_additions()
            if additions:
                parts.append(f"\n{additions}")

        # Relevant facts
        if context.relevant_facts:
            fact_lines = []
            for fact in context.relevant_facts[:5]:
                fact_lines.append(
                    f"- {fact.get('subject', '')} {fact.get('predicate', '')} "
                    f"{fact.get('object', '')}"
                )
            parts.append("\nRelevant knowledge:\n" + "\n".join(fact_lines))

        return "\n".join(parts)

    def _build_messages(self, context: AgentContext) -> list[Message]:
        """Build the message history from context for the LLM."""
        messages: list[Message] = []

        # Add recent conversation history
        for msg in context.history:
            role_str = msg.get("role", "user")
            content = msg.get("content", "")
            if not content:
                continue

            if role_str == "user":
                role = Role.USER
            elif role_str == "assistant":
                role = Role.ASSISTANT
            else:
                continue

            messages.append(Message(role=role, content=content))

        return messages

    @traced("agent.handle_message")
    async def handle_message(
        self,
        message: str,
        channel_id: str,
        user_id: str,
        user_name: str,
    ) -> str:
        """Main entry point: process a user message and return a response.

        Flow:
        1. PERCEIVE: Build context (retrieve memories, preferences, system state)
        2. Get or create conversation
        3. Store user message
        4. Build system prompt and messages
        5. THINK: Send to LLM (via router) with tools
        6. ACT: If LLM returns tool_calls, execute them, feed results back
        7. Loop up to MAX_ITERATIONS until LLM returns text response
        8. REMEMBER: Store assistant response
        9. Return final text response
        """
        logger.info(
            "agent.handle_message",
            channel_id=channel_id,
            user_id=user_id,
            user_name=user_name,
            message_preview=message[:100],
        )

        # 1. PERCEIVE
        context = await build_context(
            message=message,
            channel_id=channel_id,
            user_id=user_id,
            user_name=user_name,
            retriever=self.memory_retriever,
            config=self.config,
            mesh_node=self.mesh_node,
        )

        # 2. Get or create conversation
        conv = await self.memory_store.get_active_conversation(channel_id)
        if not conv:
            conv_id = await self.memory_store.create_conversation(channel_id)
        else:
            conv_id = conv["id"]
            await self.memory_store.touch_conversation(conv_id)

        # 3. Store user message
        await self.memory_store.add_message(conv_id, "user", message, user_id, user_name)

        # 4. Build system prompt and messages
        system_prompt = self._build_system_prompt(context)
        messages = self._build_messages(context)
        messages.append(Message(role=Role.USER, content=message))

        # 5-7. Think-Act loop
        tools = self.tool_registry.get_definitions()
        response = None
        iteration = 0

        for iteration in range(self.MAX_ITERATIONS):
            logger.debug(
                "agent.think_loop",
                iteration=iteration,
                message_count=len(messages),
            )

            response = await self.llm_router.route(
                messages=messages,
                system=system_prompt,
                tools=tools if tools else None,
            )

            if not response.tool_calls:
                # Text response — we are done
                break

            # Execute each tool call and feed results back
            for tool_call in response.tool_calls:
                result = await self.executor.execute_tool_call(tool_call, user_id, channel_id)

                # Append assistant message with tool_calls
                messages.append(
                    Message(
                        role=Role.ASSISTANT,
                        content=response.content,
                        tool_calls=response.tool_calls,
                    )
                )

                # Append tool result message
                tool_output = result.output if result.success else f"Error: {result.error}"
                messages.append(
                    Message(
                        role=Role.TOOL,
                        content=tool_output,
                        tool_call_id=tool_call.id,
                    )
                )

                logger.info(
                    "agent.tool_result",
                    tool=tool_call.name,
                    success=result.success,
                    iteration=iteration,
                )
        else:
            # Reached MAX_ITERATIONS without a text response
            logger.warning(
                "agent.max_iterations_reached",
                max_iterations=self.MAX_ITERATIONS,
            )

        # Extract final response text
        final_text = ""
        if response is not None:
            final_text = response.content or ""

        if not final_text:
            final_text = "I was unable to generate a response. Please try again."

        # 8. REMEMBER
        await self.memory_store.add_message(conv_id, "assistant", final_text, "", "Turing")

        # Optionally trigger pattern extraction
        self._interaction_count += 1
        if (
            self.config.learning_auto_extract
            and self._interaction_count % self.config.learning_extract_interval == 0
        ):
            task = asyncio.create_task(self._extract_patterns(conv_id))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        logger.info(
            "agent.response_complete",
            response_length=len(final_text),
            iterations=iteration + 1 if response else 0,
        )

        return final_text

    async def _extract_patterns(self, conversation_id: str) -> None:
        """Extract patterns from recent conversation in the background."""
        try:
            from turing.learning.patterns import PatternExtractor

            extractor = PatternExtractor(self.llm_router, self.memory_store)
            await extractor.extract(conversation_id)
        except Exception as exc:
            logger.warning("agent.pattern_extraction_failed", error=str(exc))
