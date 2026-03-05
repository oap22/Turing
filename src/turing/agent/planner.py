"""Multi-step task planner using LLM decomposition."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from turing.llm.base import Message, Role

if TYPE_CHECKING:
    from turing.llm.base import ToolDefinition
    from turing.llm.router import LLMRouter

logger = structlog.get_logger("turing.agent.planner")


@dataclass
class PlanStep:
    """A single step within a plan."""

    description: str
    tool_name: str | None = None
    tool_args: dict = field(default_factory=dict)
    status: str = "pending"  # pending, in_progress, completed, failed
    result: str = ""


@dataclass
class Plan:
    """An ordered sequence of steps to accomplish a goal."""

    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    current_step: int = 0

    @property
    def is_complete(self) -> bool:
        """Whether all steps have been executed."""
        return self.current_step >= len(self.steps)

    @property
    def current(self) -> PlanStep | None:
        """The current step to execute, or None if complete."""
        if self.is_complete:
            return None
        return self.steps[self.current_step]

    def advance(self) -> None:
        """Move to the next step."""
        if not self.is_complete:
            self.current_step += 1

    def mark_current(self, status: str, result: str = "") -> None:
        """Mark the current step with a status and optional result."""
        step = self.current
        if step is not None:
            step.status = status
            step.result = result

    @property
    def summary(self) -> str:
        """Return a human-readable summary of the plan."""
        lines = [f"Goal: {self.goal}"]
        for i, step in enumerate(self.steps):
            marker = ">" if i == self.current_step else " "
            status_icon = {
                "pending": "[ ]",
                "in_progress": "[~]",
                "completed": "[x]",
                "failed": "[!]",
            }.get(step.status, "[ ]")
            tool_info = f" (tool: {step.tool_name})" if step.tool_name else ""
            lines.append(f"  {marker} {status_icon} Step {i + 1}: {step.description}{tool_info}")
        return "\n".join(lines)


# Keywords/patterns that suggest multi-step work
_MULTI_STEP_PATTERNS = [
    r"\bthen\b",
    r"\bafter that\b",
    r"\bfirst\b.*\bthen\b",
    r"\bstep\s*\d",
    r"\band\s+also\b",
    r"\bfollow(?:ed)?\s+by\b",
    r"\bnext\b",
    r"\bfinally\b",
    r"\bset\s*up\b.*\band\b",
    r"\bcreate\b.*\band\b.*\bconfigure\b",
    r"\binstall\b.*\band\b.*\bsetup\b",
]


class Planner:
    """Decomposes multi-step tasks into ordered steps using LLM analysis."""

    def __init__(self, llm_router: LLMRouter) -> None:
        self._llm_router = llm_router

    async def should_plan(self, message: str) -> bool:
        """Determine if a message requires multi-step planning.

        Uses a heuristic approach: checks for indicators of multi-step
        tasks such as sequencing words, numbered lists, and compound
        requests.
        """
        lowered = message.lower()

        # Check for multi-step patterns
        for pattern in _MULTI_STEP_PATTERNS:
            if re.search(pattern, lowered):
                return True

        # Check for numbered/bulleted lists
        if re.search(r"(?m)^\s*(?:\d+[\.\)]\s|[-*]\s)", message):
            return True

        # Check for multiple question marks (multiple questions)
        if message.count("?") >= 2:
            return True

        return False

    async def create_plan(
        self,
        goal: str,
        available_tools: list[ToolDefinition],
    ) -> Plan:
        """Use LLM to decompose a goal into executable steps.

        Sends the goal and available tools to the LLM and parses the
        structured response into a Plan with PlanStep objects.
        """
        tool_descriptions = "\n".join(
            f"- {t.name}: {t.description}" for t in available_tools
        )

        planning_prompt = (
            "You are a task planner. Break down the following goal into concrete, "
            "ordered steps. For each step that requires a tool, specify which tool to use.\n\n"
            f"Available tools:\n{tool_descriptions}\n\n"
            f"Goal: {goal}\n\n"
            "Respond with a JSON array of steps. Each step should have:\n"
            '- "description": what to do\n'
            '- "tool_name": the tool to use (null if no tool needed)\n'
            '- "tool_args": arguments for the tool as an object (empty object if no tool)\n\n'
            "Example response:\n"
            "```json\n"
            "[\n"
            '  {"description": "List files in the directory", "tool_name": "shell", '
            '"tool_args": {"command": "ls -la"}},\n'
            '  {"description": "Summarize the findings", "tool_name": null, "tool_args": {}}\n'
            "]\n"
            "```\n\n"
            "Respond ONLY with the JSON array, no other text."
        )

        messages = [Message(role=Role.USER, content=planning_prompt)]

        try:
            response = await self._llm_router.route(
                messages=messages,
                system="You are a task planning assistant. Respond only with valid JSON.",
                max_tokens=2048,
                temperature=0.3,
            )

            steps = self._parse_plan_response(response.content)
            plan = Plan(goal=goal, steps=steps)

            logger.info(
                "plan_created",
                goal=goal[:100],
                step_count=len(steps),
            )

            return plan

        except Exception as exc:
            logger.error("plan_creation_failed", error=str(exc), goal=goal[:100])
            # Return a single-step fallback plan
            return Plan(
                goal=goal,
                steps=[
                    PlanStep(
                        description=goal,
                        tool_name=None,
                        tool_args={},
                    )
                ],
            )

    def _parse_plan_response(self, response_text: str) -> list[PlanStep]:
        """Parse the LLM's response into PlanStep objects."""
        # Try to extract JSON from the response (may be wrapped in code fences)
        json_text = response_text.strip()

        # Remove markdown code fences if present
        code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)```", json_text, re.DOTALL)
        if code_block_match:
            json_text = code_block_match.group(1).strip()

        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            logger.warning("plan_parse_failed", response=response_text[:200])
            return [PlanStep(description="Execute the task as described")]

        if not isinstance(parsed, list):
            logger.warning("plan_not_a_list", response_type=type(parsed).__name__)
            return [PlanStep(description="Execute the task as described")]

        steps: list[PlanStep] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            steps.append(
                PlanStep(
                    description=item.get("description", ""),
                    tool_name=item.get("tool_name"),
                    tool_args=item.get("tool_args", {}),
                )
            )

        if not steps:
            steps = [PlanStep(description="Execute the task as described")]

        return steps
