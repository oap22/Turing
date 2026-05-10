"""SpendTracker — per-day cloud-LLM spend with per-task attribution.

The tracker stores raw ``(task_id, cents, when)`` tuples and computes the
current day's total at read time. There's no scheduled cleanup — the
day boundary check is recomputed on every call so a midnight rollover
naturally resets ``spent_today`` without any background task.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class _Record:
    when: datetime
    task_id: str
    cents: int


class SpendTracker:
    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._records: list[_Record] = []

    def record(self, *, task_id: str, cents: int) -> None:
        self._records.append(_Record(when=self._now(), task_id=task_id, cents=cents))

    def spent_today(self) -> int:
        today = self._today()
        return sum(r.cents for r in self._records if r.when.date() == today)

    def spent_for_task(self, task_id: str) -> int:
        return sum(r.cents for r in self._records if r.task_id == task_id)

    def _today(self) -> date:
        return self._now().date()
