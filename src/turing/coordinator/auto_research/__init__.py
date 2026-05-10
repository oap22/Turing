"""Auto-research scheduler: per-specialty standing goals run during idle nights."""

from __future__ import annotations

from turing.coordinator.auto_research.goal import Goal
from turing.coordinator.auto_research.inbox import inbox_directory_for
from turing.coordinator.auto_research.local_only import (
    AutoResearchCloudRefusedError,
    LocalOnlyBudgetGuard,
)
from turing.coordinator.auto_research.registry import GoalsRegistry
from turing.coordinator.auto_research.scheduler import GoalScheduler

__all__ = [
    "AutoResearchCloudRefusedError",
    "Goal",
    "GoalScheduler",
    "GoalsRegistry",
    "LocalOnlyBudgetGuard",
    "inbox_directory_for",
]
