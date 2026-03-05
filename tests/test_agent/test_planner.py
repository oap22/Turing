"""Tests for the multi-step Planner."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from turing.agent.planner import Plan, PlanStep, Planner
from turing.llm.base import LLMResponse, ToolDefinition


# ---------------------------------------------------------------------------
# Plan / PlanStep unit tests
# ---------------------------------------------------------------------------


def test_plan_is_complete_empty():
    """An empty plan should be considered complete."""
    plan = Plan(goal="test", steps=[])
    assert plan.is_complete is True
    assert plan.current is None


def test_plan_step_advancement():
    """Plan should track current step and advance correctly."""
    steps = [
        PlanStep(description="Step 1"),
        PlanStep(description="Step 2"),
        PlanStep(description="Step 3"),
    ]
    plan = Plan(goal="multi-step", steps=steps)

    assert plan.is_complete is False
    assert plan.current is steps[0]
    assert plan.current_step == 0

    plan.advance()
    assert plan.current is steps[1]
    assert plan.current_step == 1

    plan.advance()
    assert plan.current is steps[2]
    assert plan.current_step == 2

    plan.advance()
    assert plan.is_complete is True
    assert plan.current is None


def test_plan_mark_current():
    """Marking the current step should update its status and result."""
    plan = Plan(
        goal="test",
        steps=[PlanStep(description="Do something")],
    )

    plan.mark_current("completed", "Done successfully")
    assert plan.steps[0].status == "completed"
    assert plan.steps[0].result == "Done successfully"


def test_plan_summary():
    """Plan summary should include goal and step details."""
    plan = Plan(
        goal="Deploy app",
        steps=[
            PlanStep(description="Build", tool_name="shell", status="completed"),
            PlanStep(description="Test", tool_name="shell", status="in_progress"),
            PlanStep(description="Deploy", tool_name=None, status="pending"),
        ],
        current_step=1,
    )

    summary = plan.summary
    assert "Deploy app" in summary
    assert "Build" in summary
    assert "Test" in summary
    assert "Deploy" in summary
    assert "(tool: shell)" in summary


# ---------------------------------------------------------------------------
# should_plan heuristic tests
# ---------------------------------------------------------------------------


async def test_should_plan_with_multi_step_keywords():
    """Messages with multi-step keywords should trigger planning."""
    planner = Planner(AsyncMock())

    assert await planner.should_plan("First install Python then configure it") is True
    assert await planner.should_plan("Set up the server and configure the firewall") is True
    assert await planner.should_plan("After that, restart the service") is True


async def test_should_plan_with_numbered_list():
    """Messages with numbered lists should trigger planning."""
    planner = Planner(AsyncMock())

    msg = "1. Install packages\n2. Configure settings\n3. Start service"
    assert await planner.should_plan(msg) is True


async def test_should_plan_with_multiple_questions():
    """Messages with multiple question marks should trigger planning."""
    planner = Planner(AsyncMock())

    assert await planner.should_plan("What is the CPU usage? What is the memory?") is True


async def test_should_not_plan_simple_message():
    """Simple messages should not trigger planning."""
    planner = Planner(AsyncMock())

    assert await planner.should_plan("Hello") is False
    assert await planner.should_plan("What time is it") is False
    assert await planner.should_plan("Show me the logs") is False


# ---------------------------------------------------------------------------
# create_plan tests
# ---------------------------------------------------------------------------


async def test_create_plan_success():
    """Planner should parse a valid LLM response into a Plan."""
    steps_json = json.dumps([
        {"description": "List files", "tool_name": "shell", "tool_args": {"command": "ls"}},
        {"description": "Analyze output", "tool_name": None, "tool_args": {}},
    ])

    llm_router = AsyncMock()
    llm_router.route.return_value = LLMResponse(
        content=steps_json,
        tool_calls=[],
        model="test",
    )

    planner = Planner(llm_router)
    tools = [
        ToolDefinition(
            name="shell",
            description="Run commands",
            parameters={"type": "object"},
        )
    ]

    plan = await planner.create_plan("List and analyze files", tools)

    assert plan.goal == "List and analyze files"
    assert len(plan.steps) == 2
    assert plan.steps[0].tool_name == "shell"
    assert plan.steps[0].tool_args == {"command": "ls"}
    assert plan.steps[1].tool_name is None
    assert plan.is_complete is False


async def test_create_plan_with_code_fences():
    """Planner should handle responses wrapped in markdown code fences."""
    steps_json = (
        "```json\n"
        + json.dumps([
            {"description": "Check status", "tool_name": "system_info", "tool_args": {}},
        ])
        + "\n```"
    )

    llm_router = AsyncMock()
    llm_router.route.return_value = LLMResponse(
        content=steps_json,
        tool_calls=[],
        model="test",
    )

    planner = Planner(llm_router)
    plan = await planner.create_plan("Check system status", [])

    assert len(plan.steps) == 1
    assert plan.steps[0].tool_name == "system_info"


async def test_create_plan_fallback_on_invalid_json():
    """If LLM returns invalid JSON, planner should create a single fallback step."""
    llm_router = AsyncMock()
    llm_router.route.return_value = LLMResponse(
        content="This is not valid JSON",
        tool_calls=[],
        model="test",
    )

    planner = Planner(llm_router)
    plan = await planner.create_plan("Do something complex", [])

    assert len(plan.steps) == 1
    assert plan.steps[0].tool_name is None


async def test_create_plan_fallback_on_llm_error():
    """If LLM call fails, planner should create a single fallback step."""
    llm_router = AsyncMock()
    llm_router.route.side_effect = RuntimeError("LLM unavailable")

    planner = Planner(llm_router)
    plan = await planner.create_plan("Deploy application", [])

    assert len(plan.steps) == 1
    assert plan.goal == "Deploy application"
