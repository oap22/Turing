"""LocalOnlyBudgetGuard — refuse cloud LLM calls from auto-research paths.

Phase 1 auto-research runs exclusively on local models. This guard sits at
the boundary so a misconfigured prompt or rogue tool that tries to upgrade
to cloud cannot quietly succeed.
"""

from __future__ import annotations


class AutoResearchCloudRefusedError(RuntimeError):
    """Raised when an auto-research path attempts a cloud LLM call."""


class LocalOnlyBudgetGuard:
    def check(self, *, provider: str) -> None:
        if provider != "local":
            raise AutoResearchCloudRefusedError(
                f"auto-research paths must use provider='local', got {provider!r}"
            )
