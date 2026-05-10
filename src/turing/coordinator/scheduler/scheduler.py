"""Dispatch decisions: given a subtask and the registry, pick a worker.

Decision rule (matches PRD user stories 8, 10, 11):

    in_flight < max_concurrent
      AND specialty_required ∈ manifest.specialties
      AND required_tools ⊆ manifest.tools
      then break ties by highest eval_score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from turing.coordinator.registry import CapabilityRegistry
    from turing.coordinator.registry.manifest import CapabilityManifest


@dataclass(frozen=True)
class Subtask:
    subtask_id: str
    specialty_required: str
    required_tools: tuple[str, ...] = ()


class Scheduler:
    def pick(self, subtask: Subtask, registry: CapabilityRegistry) -> CapabilityManifest | None:
        candidates = registry.find_workers(
            specialty=subtask.specialty_required,
            required_tools=subtask.required_tools,
            exclude_busy=True,
        )
        if not candidates:
            return None
        return max(candidates, key=lambda m: m.eval_score)

    def pick_for_retry(
        self,
        subtask: Subtask,
        registry: CapabilityRegistry,
        *,
        exclude_worker_ids: Iterable[str],
    ) -> CapabilityManifest | None:
        """Pick a worker for a retry, refusing any in `exclude_worker_ids`.

        Returns None when no eligible alternate worker exists, signalling
        that the subtask should be marked terminal rather than retried.
        """
        excluded = frozenset(exclude_worker_ids)
        candidates = [
            m
            for m in registry.find_workers(
                specialty=subtask.specialty_required,
                required_tools=subtask.required_tools,
                exclude_busy=True,
            )
            if m.worker_id not in excluded
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda m: m.eval_score)
