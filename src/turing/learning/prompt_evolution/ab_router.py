"""ABRouter — split each specialty's traffic between baseline and candidate.

The routing decision is deterministic per ``task_id`` so a retry of the same
task lands on the same arm; otherwise our A/B contamination would invalidate
the gate's win-rate measurement.
"""

from __future__ import annotations

import hashlib

from turing.learning.prompt_evolution.registry import PromptVersionRegistry


class ABRouter:
    def __init__(
        self,
        *,
        registry: PromptVersionRegistry,
        candidate_share: float,
    ) -> None:
        if not 0.0 <= candidate_share <= 1.0:
            raise ValueError(
                f"candidate_share must be in [0, 1], got {candidate_share!r}"
            )
        self._registry = registry
        self._share = candidate_share

    def choose(self, specialty: str, *, task_id: str) -> str:
        baseline = self._registry.baseline_for(specialty)
        candidate = self._registry.candidate_for(specialty)
        if baseline is None:
            raise KeyError(f"no baseline registered for {specialty!r}")
        if candidate is None or self._share <= 0.0:
            return baseline
        if self._share >= 1.0:
            return candidate
        # Deterministic hash on task_id keeps retries on the same arm.
        digest = hashlib.sha256(f"{specialty}:{task_id}".encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") / 2**64
        return candidate if bucket < self._share else baseline
