"""Tests for the goals.yaml auto-research scheduler (#20)."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from turing.coordinator.auto_research import (
    AutoResearchCloudRefused,
    Goal,
    GoalScheduler,
    GoalsRegistry,
    LocalOnlyBudgetGuard,
    inbox_directory_for,
)


def _now(hour: int = 3, day: int = 7) -> datetime:
    return datetime(2026, 5, day, hour, 0, tzinfo=UTC)


# ── Goal + GoalsRegistry ─────────────────────────────────────────────


class TestGoalRegistry:
    def test_loads_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "goals.yaml"
        path.write_text(
            textwrap.dedent(
                """\
                goals:
                  - specialty: research-summarize
                    prompt: "summarise recent papers"
                    cadence_hours: 24
                    monthly_budget_cents: 0
                    required_tools: ["vault_query", "web_fetch"]
                  - specialty: code-debug
                    prompt: "review yesterday's tracebacks"
                    cadence_hours: 168
                    monthly_budget_cents: 50
                    required_tools: []
                """
            ),
            encoding="utf-8",
        )
        registry = GoalsRegistry.load(path)
        assert len(registry.goals) == 2
        first = registry.goals[0]
        assert first.specialty == "research-summarize"
        assert first.cadence_hours == 24
        assert "vault_query" in first.required_tools

    def test_missing_required_field_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "goals.yaml"
        path.write_text("goals:\n  - specialty: x\n    prompt: y\n", encoding="utf-8")
        with pytest.raises(ValueError, match="cadence_hours"):
            GoalsRegistry.load(path)


# ── GoalScheduler.due_goals ──────────────────────────────────────────


def _goal(
    *,
    specialty: str = "research-summarize",
    cadence_hours: int = 24,
    monthly_budget_cents: int = 0,
) -> Goal:
    return Goal(
        specialty=specialty,
        prompt="x",
        cadence_hours=cadence_hours,
        monthly_budget_cents=monthly_budget_cents,
        required_tools=("vault_query",),
    )


class TestSchedulerWindow:
    def test_fires_inside_idle_window(self) -> None:
        scheduler = GoalScheduler(
            min_idle_seconds=600,
            allowed_hour_range=(2, 6),
        )
        due = scheduler.due_goals(
            goals=[_goal()],
            now=_now(hour=3),
            idle_seconds=900,
            last_fired={},
            monthly_spent_by_specialty={},
        )
        assert len(due) == 1

    def test_blocks_when_not_idle_enough(self) -> None:
        scheduler = GoalScheduler(
            min_idle_seconds=600,
            allowed_hour_range=(2, 6),
        )
        due = scheduler.due_goals(
            goals=[_goal()],
            now=_now(hour=3),
            idle_seconds=120,  # only 2 min idle
            last_fired={},
            monthly_spent_by_specialty={},
        )
        assert due == []

    def test_blocks_outside_clock_window(self) -> None:
        scheduler = GoalScheduler(min_idle_seconds=60, allowed_hour_range=(2, 6))
        due = scheduler.due_goals(
            goals=[_goal()],
            now=_now(hour=14),  # 2 PM
            idle_seconds=10_000,
            last_fired={},
            monthly_spent_by_specialty={},
        )
        assert due == []


class TestCadence:
    def test_skips_within_cadence(self) -> None:
        scheduler = GoalScheduler(min_idle_seconds=60, allowed_hour_range=(2, 6))
        now = _now(hour=3)
        last = {"research-summarize": now - timedelta(hours=1)}
        due = scheduler.due_goals(
            goals=[_goal(cadence_hours=24)],
            now=now,
            idle_seconds=900,
            last_fired=last,
            monthly_spent_by_specialty={},
        )
        assert due == []

    def test_fires_after_cadence_elapsed(self) -> None:
        scheduler = GoalScheduler(min_idle_seconds=60, allowed_hour_range=(2, 6))
        now = _now(hour=3)
        last = {"research-summarize": now - timedelta(hours=25)}
        due = scheduler.due_goals(
            goals=[_goal(cadence_hours=24)],
            now=now,
            idle_seconds=900,
            last_fired=last,
            monthly_spent_by_specialty={},
        )
        assert len(due) == 1


class TestMonthlyCap:
    def test_blocks_when_monthly_cap_exhausted(self) -> None:
        scheduler = GoalScheduler(min_idle_seconds=60, allowed_hour_range=(2, 6))
        due = scheduler.due_goals(
            goals=[_goal(monthly_budget_cents=100)],
            now=_now(hour=3),
            idle_seconds=900,
            last_fired={},
            monthly_spent_by_specialty={"research-summarize": 100},
        )
        assert due == []

    def test_allows_when_monthly_budget_not_set_zero(self) -> None:
        """A budget of 0 means unlimited (Phase 1: local-only, free)."""
        scheduler = GoalScheduler(min_idle_seconds=60, allowed_hour_range=(2, 6))
        due = scheduler.due_goals(
            goals=[_goal(monthly_budget_cents=0)],
            now=_now(hour=3),
            idle_seconds=900,
            last_fired={},
            monthly_spent_by_specialty={"research-summarize": 100_000},
        )
        assert len(due) == 1


# ── LocalOnlyBudgetGuard ──────────────────────────────────────────────


class TestLocalOnlyBudgetGuard:
    def test_allows_local_calls(self) -> None:
        guard = LocalOnlyBudgetGuard()
        guard.check(provider="local")  # must not raise

    def test_rejects_cloud_calls(self) -> None:
        guard = LocalOnlyBudgetGuard()
        with pytest.raises(AutoResearchCloudRefused):
            guard.check(provider="cloud")


# ── inbox path helper ────────────────────────────────────────────────


class TestInboxDirectory:
    def test_path_includes_specialty_and_date(self) -> None:
        path = inbox_directory_for(specialty="research-summarize", at=_now(day=7))
        assert path == Path("vault/inbox/auto_research/research-summarize/2026-05-07")

    def test_specialty_with_slashes_rejected(self) -> None:
        with pytest.raises(ValueError):
            inbox_directory_for(specialty="../escape", at=_now())
