"""Canary REJECTED side-effects: hard-examples writeback + Discord notify.

Issue #118 follow-up. Wires `CanaryGateRunner.on_rejected` to:

  * `archive_failed_training` — convert each failed_case from the worker's
    eval payload into a `HardExample` row + post the existing notifier
    summary (one-liner from `hard_examples.py`).
  * A formatted multi-line Discord message body for the live-DAG channel
    so the operator sees the regression context at a glance.

The 30-day regression-rate count is read from a `RejectionLog` — a tiny
stateful sidecar so the runner doesn't have to inflate `AdapterRegistry`
with timestamp tracking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from turing.coordinator.promotion.hard_examples import (
    archive_failed_training,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from turing.coordinator.lifecycle.episode_store import EpisodeStore
    from turing.coordinator.promotion.canary_gate_runner import CanaryGateOutcome
    from turing.coordinator.promotion.hard_examples import _Notifier

REGRESSION_HORIZON_MS = 30 * 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class _RejectionRow:
    specialty: str
    name: str
    version: str
    ts_ms: int


class RejectionLog:
    """In-memory specialty → rejection-timestamp index.

    Mirrors the ADR 0007 §6 `regressions_30d(specialty)` query that #I will
    persist. Lives in process for v1; the on-rejected handler appends to it
    and the notification body reads back from it.
    """

    def __init__(self) -> None:
        self._rows: list[_RejectionRow] = []

    def record(self, *, specialty: str, name: str, version: str, ts_ms: int) -> None:
        self._rows.append(_RejectionRow(specialty, name, version, ts_ms))

    def count_within(self, *, specialty: str, now_ms: int, horizon_ms: int) -> int:
        cutoff = now_ms - horizon_ms
        return sum(1 for r in self._rows if r.specialty == specialty and r.ts_ms >= cutoff)


def format_rejection_notice(
    *,
    name: str,
    version: str,
    specialty: str,
    outcome: CanaryGateOutcome,
    prior_name: str | None,
    prior_version: str | None,
    regressions_30d: int,
) -> str:
    """ADR 0007 §3 message body for the live-DAG channel."""
    delta = f"{outcome.delta_pp:+.2f}pp" if outcome.delta_pp is not None else "n/a"
    canary_score = f"{outcome.score:.2f}" if outcome.score is not None else "n/a"
    prior_score = "n/a"
    prior_label = (
        f"{prior_name}:{prior_version}" if prior_name and prior_version else "(no prior LIVE)"
    )
    return (
        f"Adapter `{name}:{version}` failed canary on `{specialty}`.\n"
        f"  Δ score: {delta} (canary {canary_score} vs. prior {prior_score})\n"
        f"  Reverted to `{prior_label}`.\n"
        f"  Hard-examples batch: {len(outcome.hard_examples)} cases.\n"
        f"  Regressions this month for `{specialty}`: {regressions_30d}."
    )


def make_canary_rejected_handler(
    *,
    episode_store: EpisodeStore,
    rejection_log: RejectionLog,
    notifier: _Notifier,
    discord_post: Callable[[str], Awaitable[None]] | None,
    now_ms: Callable[[], int],
) -> Callable[[CanaryGateOutcome, str, str, str], Awaitable[None]]:
    """Return a coroutine matching `CanaryGateRunner.on_rejected` signature."""

    async def on_rejected(
        outcome: CanaryGateOutcome,
        name: str,
        version: str,
        specialty: str,
    ) -> None:
        ts = now_ms()
        rejection_log.record(specialty=specialty, name=name, version=version, ts_ms=ts)

        if outcome.hard_examples:
            archive_failed_training(
                run_id=f"canary-{name}-{version}",
                specialty=specialty,
                examples=outcome.hard_examples,
                episode_store=episode_store,
                notifier=notifier,
            )

        if discord_post is not None:
            count = rejection_log.count_within(
                specialty=specialty, now_ms=ts, horizon_ms=REGRESSION_HORIZON_MS
            )
            body = format_rejection_notice(
                name=name,
                version=version,
                specialty=specialty,
                outcome=outcome,
                prior_name=None,
                prior_version=None,
                regressions_30d=count,
            )
            await discord_post(body)

    return on_rejected
