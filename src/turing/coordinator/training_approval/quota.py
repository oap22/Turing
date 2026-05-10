"""QuotaTracker — per-specialty rolling 7-day quota for training jobs.

Two caps per specialty: ``jobs_per_week`` and ``dollars_per_week``. Records age
out of the window automatically — there is no scheduled cleanup, the rolling
window is computed at check time so the tracker has no background state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

WEEK = timedelta(days=7)


@dataclass(frozen=True)
class SpecialtyQuota:
    jobs_per_week: int
    dollars_per_week: float


@dataclass
class _Record:
    when: datetime
    cost_usd: float


class QuotaTracker:
    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._quotas: dict[str, SpecialtyQuota] = {}
        self._records: dict[str, list[_Record]] = {}
        self._now = now or (lambda: datetime.now(tz=UTC))

    def set_quota(self, specialty: str, quota: SpecialtyQuota) -> None:
        self._quotas[specialty] = quota

    def record(self, specialty: str, *, cost_usd: float) -> None:
        bucket = self._records.setdefault(specialty, [])
        bucket.append(_Record(when=self._now(), cost_usd=cost_usd))

    def check(self, specialty: str, *, cost_usd: float) -> tuple[bool, str]:
        """Return (ok, reason). ``ok`` is True when the proposed cost fits."""
        quota = self._quotas.get(specialty)
        if quota is None:
            return True, ""
        cutoff = self._now() - WEEK
        active = [r for r in self._records.get(specialty, ()) if r.when >= cutoff]
        if len(active) >= quota.jobs_per_week:
            return False, (f"jobs/week quota exhausted: {len(active)}/{quota.jobs_per_week}")
        spent = sum(r.cost_usd for r in active)
        if spent + cost_usd > quota.dollars_per_week:
            return False, (
                f"$/week quota would be exceeded: ${spent:.2f}+${cost_usd:.2f} "
                f"> ${quota.dollars_per_week:.2f}"
            )
        return True, ""
