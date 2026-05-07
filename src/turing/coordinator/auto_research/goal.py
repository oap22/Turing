"""Goal — a single standing per-specialty research target."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Goal:
    specialty: str
    prompt: str
    cadence_hours: int
    monthly_budget_cents: int
    required_tools: tuple[str, ...] = ()
