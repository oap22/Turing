"""GoalScheduler — decide which goals fire NOW.

Three independent gates each goal must pass:

1. **Idle window**: cluster idle ≥ ``min_idle_seconds``.
2. **Wall-clock window**: ``now.hour`` falls inside ``allowed_hour_range``.
3. **Cadence + monthly cap**: at least ``cadence_hours`` since last fire,
   and (if a non-zero monthly cap is set) the running spend hasn't already
   exhausted it.

The scheduler is pure — it returns the list of due goals; the caller is
responsible for actually dispatching them and recording their cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from turing.coordinator.auto_research.goal import Goal


@dataclass(frozen=True)
class GoalScheduler:
    min_idle_seconds: int
    allowed_hour_range: tuple[int, int]

    def due_goals(
        self,
        *,
        goals: Iterable[Goal],
        now: datetime,
        idle_seconds: int,
        last_fired: Mapping[str, datetime],
        monthly_spent_by_specialty: Mapping[str, int],
    ) -> list[Goal]:
        if idle_seconds < self.min_idle_seconds:
            return []
        start, end = self.allowed_hour_range
        if not start <= now.hour < end:
            return []
        out: list[Goal] = []
        for goal in goals:
            last = last_fired.get(goal.specialty)
            if last is not None and now - last < timedelta(hours=goal.cadence_hours):
                continue
            cap = goal.monthly_budget_cents
            spent = monthly_spent_by_specialty.get(goal.specialty, 0)
            if cap > 0 and spent >= cap:
                continue
            out.append(goal)
        return out
