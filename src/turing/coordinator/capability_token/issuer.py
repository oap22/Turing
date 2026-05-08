"""TokenIssuer — high-level wrapper that builds v1 deny-by-default tokens.

Per ADR 0003 §2 (deny-by-default) and §6 (token shape), the orchestrator
attaches one of these to every ``SubtaskDispatch`` it publishes. The
``allowed_commands_regex`` is the unmatchable pattern in v1; future slices
flip it on a per-specialty basis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turing.coordinator.capability_token.scope import CapabilityScope
from turing.coordinator.capability_token.token import (
    CapabilityToken,
    CapabilityTokenIssuer,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from turing.coordinator.dispatch.envelopes import SubtaskDispatch
    from turing.coordinator.planner.schema import Subtask
    from turing.transport.signer import MessageSigner


# Matches no string. Picked so a future audit grep finds the v1 marker.
DENY_ALL_REGEX = r"(?!x)x"

# Per ADR 0003 §4 — per-call shell cap, defaults to 30s, capped at the
# subtask's own timeout. Token TTL gets a 30s grace beyond dispatch deadline.
DEFAULT_TIMEOUT_S = 30
EXPIRY_GRACE_MS = 30_000


class TokenIssuer:
    def __init__(
        self,
        *,
        signer: MessageSigner,
        now_ms: Callable[[], int],
        allowed_commands_regex: str = DENY_ALL_REGEX,
    ) -> None:
        self._inner = CapabilityTokenIssuer(signer=signer)
        self._now_ms = now_ms
        self._regex = allowed_commands_regex

    def issue_for(self, *, subtask: Subtask, dispatch: SubtaskDispatch) -> CapabilityToken:
        scope = CapabilityScope(
            subtask_id=dispatch.subtask_id,
            task_id=dispatch.task_id,
            allowed_commands_regex=self._regex,
            timeout_s=min(DEFAULT_TIMEOUT_S, subtask.timeout_s),
            expires_at_ms=dispatch.deadline_ms + EXPIRY_GRACE_MS,
            issued_at_ms=self._now_ms(),
        )
        return self._inner.issue(scope)
