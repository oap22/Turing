"""PromptVersionRegistry — per-specialty baseline + candidate prompt versions."""

from __future__ import annotations


class PromptVersionRegistry:
    def __init__(self) -> None:
        self._baseline: dict[str, str] = {}
        self._candidate: dict[str, str] = {}

    def set_baseline(self, specialty: str, version: str) -> None:
        self._baseline[specialty] = version

    def set_candidate(self, specialty: str, version: str) -> None:
        self._candidate[specialty] = version

    def baseline_for(self, specialty: str) -> str | None:
        return self._baseline.get(specialty)

    def candidate_for(self, specialty: str) -> str | None:
        return self._candidate.get(specialty)

    def promote(self, specialty: str) -> str:
        """Promote candidate to baseline; return the new baseline."""
        candidate = self._candidate.pop(specialty, None)
        if candidate is None:
            raise KeyError(f"no candidate for {specialty!r}")
        self._baseline[specialty] = candidate
        return candidate
