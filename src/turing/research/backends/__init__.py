"""Model-backend seam: send messages + tool definitions, get a response.

One interface with a Claude implementation today and a local implementation
droppable in later. The brief pins one engine family deliberately — a shifting
cloud/local mix would produce round-over-round deltas unrelated to the
scaffold — so the local backend is a seam, not a participant.

Layout:

* :mod:`~turing.research.backends.protocol` — the neutral interface and its
  types. Anything vendor-specific appearing there is a defect.
* :mod:`~turing.research.backends.tiering` — the two tiers and the policy
  mapping them onto concrete models.
* :mod:`~turing.research.backends.accounting` — token, step, and wall-clock
  accounting. The cap reads this; under-counting here disables the brake.
* :mod:`~turing.research.backends.base` — accounting, checkpointing, and
  logging every implementation inherits.
* :mod:`~turing.research.backends.claude` — the real backend.
* :mod:`~turing.research.backends.local` — documented stub; raises.
* :mod:`~turing.research.backends.fake` — scripted backend for tests.
* :mod:`~turing.research.backends.adapter` — :class:`~turing.research.solver.protocols.ProposalBackend`
  over a :class:`~turing.research.backends.protocol.ModelBackend`. The one
  sanctioned import from this package into solver types.

``ClaudeBackend`` is **not** re-exported here: importing it drags in
pydantic-settings and the vendor SDK, and a module that only needs the protocol
types should not pay for that. Import it from
:mod:`turing.research.backends.claude` directly.
"""

from __future__ import annotations

from turing.research.backends.accounting import (
    CONSERVATIVE_CHARS_PER_TOKEN,
    DIGIT_CHARS_PER_TOKEN,
    MESSAGE_TOKEN_OVERHEAD,
    NON_ASCII_CHARS_PER_TOKEN,
    TOOL_TOKEN_OVERHEAD,
    LedgerSnapshot,
    Usage,
    UsageLedger,
    estimate_tokens,
)
from turing.research.backends.adapter import ProposalAdapter, encode_proposal
from turing.research.backends.base import (
    BaseBackend,
    RawTurn,
    estimate_request_tokens,
    estimate_turn_output_tokens,
)
from turing.research.backends.errors import (
    BackendAccountingError,
    BackendCapacityError,
    BackendConfigurationError,
    BackendContextLengthError,
    BackendError,
    BackendNotImplementedError,
    BackendProtocolError,
    BackendResumeError,
    ScriptExhaustedError,
)
from turing.research.backends.fake import DeterministicClock, FakeBackend, ScriptedTurn
from turing.research.backends.local import LocalBackend
from turing.research.backends.protocol import (
    RESUME_TOKEN_VERSION,
    BackendIdentity,
    GenerationRequest,
    GenerationResponse,
    ModelBackend,
    ModelMessage,
    ResumeToken,
    Role,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from turing.research.backends.tiering import ModelTier, TieringPolicy

__all__ = [
    "CONSERVATIVE_CHARS_PER_TOKEN",
    "DIGIT_CHARS_PER_TOKEN",
    "MESSAGE_TOKEN_OVERHEAD",
    "NON_ASCII_CHARS_PER_TOKEN",
    "RESUME_TOKEN_VERSION",
    "TOOL_TOKEN_OVERHEAD",
    "BackendAccountingError",
    "BackendCapacityError",
    "BackendConfigurationError",
    "BackendContextLengthError",
    "BackendError",
    "BackendIdentity",
    "BackendNotImplementedError",
    "BackendProtocolError",
    "BackendResumeError",
    "BaseBackend",
    "DeterministicClock",
    "FakeBackend",
    "GenerationRequest",
    "GenerationResponse",
    "LedgerSnapshot",
    "LocalBackend",
    "ModelBackend",
    "ModelMessage",
    "ModelTier",
    "ProposalAdapter",
    "RawTurn",
    "ResumeToken",
    "Role",
    "ScriptExhaustedError",
    "ScriptedTurn",
    "StopReason",
    "TieringPolicy",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "UsageLedger",
    "encode_proposal",
    "estimate_request_tokens",
    "estimate_tokens",
    "estimate_turn_output_tokens",
]
