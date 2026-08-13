"""Model tiers and the policy that maps them onto concrete models.

Two tiers, no more. The expensive tier orchestrates; the cheap tier does the
mundane sub-steps. Both are named by *role*, never by vendor or model family,
so a second backend can implement the same split with entirely different
models.

There is deliberately **no default tier** on a request. Every call site names
the tier it wants, because the whole point of the split is that routing down is
a decision someone made rather than something that happens by accident. A
default would silently make one of the two options free to ignore, and the
budget only survives if mundane work actually goes to the cheap tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from turing.research.backends.errors import BackendConfigurationError


class ModelTier(str, Enum):  # noqa: UP042
    """Which tier a request should be served by.

    ``ORCHESTRATOR`` is the capable, expensive tier: planning, writing code,
    reading verifier output, deciding what to try next. ``SUBSTEP`` is the
    cheap tier: reformatting, extracting a field, summarising a log, answering
    a yes/no question about a file.
    """

    ORCHESTRATOR = "orchestrator"
    SUBSTEP = "substep"


@dataclass(frozen=True, slots=True)
class TieringPolicy:
    """Per-tier model, output ceiling, and reasoning-effort hint.

    Immutable, so a round cannot silently re-point a tier halfway through and
    make its numbers unattributable. Changing the policy is changing the
    engine, and the engine is recorded per round.

    ``*_effort`` is an optional provider hint about how much reasoning to
    spend. It is passed through only when set; not every model accepts one, so
    the default is to say nothing and let the model's own default stand.
    """

    orchestrator_model: str
    substep_model: str
    orchestrator_max_tokens: int = 16000
    substep_max_tokens: int = 4096
    orchestrator_effort: str | None = None
    substep_effort: str | None = None

    def __post_init__(self) -> None:
        if not self.orchestrator_model or not self.substep_model:
            raise BackendConfigurationError("both tiers must name a model")
        if self.orchestrator_max_tokens <= 0 or self.substep_max_tokens <= 0:
            raise BackendConfigurationError("per-tier max_tokens must be positive")

    def model_for(self, tier: ModelTier) -> str:
        """The concrete model serving ``tier``."""
        if tier is ModelTier.ORCHESTRATOR:
            return self.orchestrator_model
        return self.substep_model

    def max_tokens_for(self, tier: ModelTier) -> int:
        """The output ceiling for ``tier`` when a request does not set one."""
        if tier is ModelTier.ORCHESTRATOR:
            return self.orchestrator_max_tokens
        return self.substep_max_tokens

    def effort_for(self, tier: ModelTier) -> str | None:
        """The reasoning-effort hint for ``tier``, or ``None`` to omit it."""
        if tier is ModelTier.ORCHESTRATOR:
            return self.orchestrator_effort
        return self.substep_effort
