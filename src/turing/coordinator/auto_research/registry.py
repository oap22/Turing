"""GoalsRegistry — load Goal definitions from a YAML file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from turing.coordinator.auto_research.goal import Goal

_REQUIRED = ("specialty", "prompt", "cadence_hours")


@dataclass(frozen=True)
class GoalsRegistry:
    goals: tuple[Goal, ...]

    @classmethod
    def load(cls, path: Path) -> GoalsRegistry:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "goals" not in raw:
            raise ValueError("goals.yaml must be a mapping with a 'goals' list")
        items = raw["goals"]
        if not isinstance(items, list):
            raise ValueError("goals.yaml 'goals' must be a list")
        return cls(goals=tuple(_parse_goal(g) for g in items))


def _parse_goal(raw: Any) -> Goal:
    if not isinstance(raw, dict):
        raise ValueError(f"goal entry must be a mapping, got {type(raw).__name__}")
    for field in _REQUIRED:
        if field not in raw:
            raise ValueError(f"goal missing required field {field!r}")
    return Goal(
        specialty=str(raw["specialty"]),
        prompt=str(raw["prompt"]),
        cadence_hours=int(raw["cadence_hours"]),
        monthly_budget_cents=int(raw.get("monthly_budget_cents", 0)),
        required_tools=tuple(str(t) for t in raw.get("required_tools", ())),
    )
