"""CanaryGateRunner — orchestrates a STAGED → LIVE/REJECTED transition.

Issue #118 / ADR 0007. On entry to STAGED for an adapter:

  1. Select canary worker via `CanarySelector` (round-robin against the
     specialty fleet).
  2. Dispatch a `SubtaskKind.CANARY_EVAL` to that worker.
  3. On `status="scored"` AND `CanaryPassGate.evaluate(...).passed`:
        registry.promote(name, version, canary_eval_score=score)
  4. Otherwise (load_failed / eval_failed / regression):
        registry.reject(name, version)
        produce HardExample rows for the worst failed cases

Fleet-wide rollout fan-out and Discord notifications are surfaced via
optional callbacks so the gate stays unit-testable; production wires the
NATS publish + Discord post on the other side.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from turing.coordinator.dispatch import (
    SourceInput,
    SubtaskDispatch,
    SubtaskKind,
)
from turing.coordinator.promotion.hard_examples import HardExample

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from turing.coordinator.adapters.registry import AdapterRegistry
    from turing.coordinator.dispatch import SubtaskDispatchClient
    from turing.coordinator.promotion.canary_pass_gate import CanaryPassGate
    from turing.coordinator.promotion.canary_selector import CanarySelector


_CANARY_DEADLINE_MS = 10 * 60 * 1000  # 10 minutes


@dataclass(frozen=True)
class CanaryGateOutcome:
    promoted: bool
    score: float | None
    delta_pp: float | None
    status: str  # "scored" | "load_failed" | "eval_failed" | "regression"
    hard_examples: tuple[HardExample, ...] = ()


class CanaryGateRunner:
    def __init__(
        self,
        *,
        registry: AdapterRegistry,
        selector: CanarySelector,
        pass_gate: CanaryPassGate,
        dispatch_client: SubtaskDispatchClient,
        now_ms: Callable[[], int],
        on_promoted: Callable[[str, str, str], Awaitable[None]] | None = None,
        on_rejected: Callable[[CanaryGateOutcome, str, str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._registry = registry
        self._selector = selector
        self._pass_gate = pass_gate
        self._client = dispatch_client
        self._now_ms = now_ms
        self._on_promoted = on_promoted
        self._on_rejected = on_rejected
        # Per-specialty round-robin state. Lives in process; #I persists it.
        self._last_canary: dict[str, str] = {}

    def last_canary_worker(self, specialty: str) -> str | None:
        return self._last_canary.get(specialty)

    async def run(
        self,
        *,
        name: str,
        version: str,
        specialty: str,
        fleet: Sequence[str],
        adapter_manifest: dict[str, Any],
        eval_set_path: str,
    ) -> CanaryGateOutcome:
        canary = self._selector.next(
            fleet=fleet, last_canary_worker_id=self._last_canary.get(specialty)
        )
        self._last_canary[specialty] = canary

        envelope = SubtaskDispatch(
            subtask_id=f"canary-{name}-{version}",
            task_id=f"canary-{specialty}",
            specialty=specialty,
            prompt=json.dumps(
                {"adapter_manifest": adapter_manifest, "eval_set_path": eval_set_path}
            ),
            source_inputs=[SourceInput(id="manifest", text=json.dumps(adapter_manifest))],
            deadline_ms=self._now_ms() + _CANARY_DEADLINE_MS,
            kind=SubtaskKind.CANARY_EVAL.value,
        )
        result = await self._client.dispatch(
            envelope,
            worker_id=canary,
            deadline_ms=envelope.deadline_ms,
        )

        outcome = self._interpret(name=name, result_output=result.output)

        if outcome.promoted:
            self._registry.promote(name=name, version=version, canary_eval_score=outcome.score)
            if self._on_promoted is not None:
                await self._on_promoted(name, version, specialty)
        else:
            self._registry.reject(name=name, version=version)
            if self._on_rejected is not None:
                await self._on_rejected(outcome, name, version, specialty)

        return outcome

    def _interpret(self, *, name: str, result_output: str) -> CanaryGateOutcome:
        body = json.loads(result_output)
        status = body.get("status", "eval_failed")
        if status != "scored":
            hard = tuple(_to_hard_example(c) for c in body.get("failed_cases", []))
            return CanaryGateOutcome(
                promoted=False,
                score=None,
                delta_pp=None,
                status=status,
                hard_examples=hard,
            )

        score = float(body["score"])
        prior = self._registry.prior_live_canary_score(name=name)
        decision = self._pass_gate.evaluate(canary_score=score, prior_live_canary_score=prior)
        if decision.passed:
            return CanaryGateOutcome(
                promoted=True, score=score, delta_pp=decision.delta_pp, status="scored"
            )
        hard = tuple(_to_hard_example(c) for c in body.get("failed_cases", []))
        return CanaryGateOutcome(
            promoted=False,
            score=score,
            delta_pp=decision.delta_pp,
            status="regression",
            hard_examples=hard,
        )


def _to_hard_example(case: dict[str, Any]) -> HardExample:
    return HardExample(
        subtask_id=str(case.get("subtask_id", "")),
        input_text=str(case.get("input_text", "")),
        expected_text=str(case.get("expected_text", "")),
        actual_output=str(case.get("actual_output", "")),
        failure_reason=str(case.get("failure_reason", "")),
    )
