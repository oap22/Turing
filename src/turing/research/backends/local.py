"""Documented stub for a local backend. Not implemented; not in loop 1.

No local model is in the loop today. The brief pins one engine family on
purpose — a round that shifted a cloud/local mix would produce a delta
unrelated to the scaffold, and pinning the engine removes that confound
entirely. What the design does require is *this seam*, so a local model can
drop in later as backup without touching the solver, the cap, or the round
record.

This module exists to hold the research that seam was chosen against, so
whoever implements it does not re-derive it.

Selected model
--------------
``mlx-community/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-4bit`` — 17.8 GB on disk,
hybrid Mamba-2 + MoE, **30B total parameters / 3B active per token**. Converted
with ``mlx-lm`` 0.31.3, which is the exact version already installed on the
target machine (M4 Pro, 10P/4E, 48 GB unified, 273 GB/s).

Expected throughput: **~80–90 tok/s**.
This is an inference, **not a measurement** — it is anchored on a published
~83 tok/s figure for the same architecture family (Nemotron 3 Nano 30B-A3B) on
identical M4 Pro / 48 GB hardware. No tokens-per-second figure has been published for this specific
model under MLX on any Apple silicon, and there is **no function-calling
benchmark for it at all**. Both claims are architectural inference.

**Prerequisite before the design depends on any of this:** benchmark it on the
machine (``mlx_lm.generate --max-tokens 128``) against the incumbent
``muse-glimmer:30b-mlx`` — a dense 30B, ~15–20 tok/s, slow because every one of
its ~29.6B parameters is active per token. That benchmark is tracked as an open
question, and it is roughly a five-minute job.

Why this model rather than the obvious rival
--------------------------------------------
``Qwen3-Coder-30B-A3B`` is the strongest alternative and the only candidate
with a hard tool-reliability number (76.9%). It measures 73.6 tok/s at 1K
context but **13.5 tok/s at 64K**. The sub-step tier's job is parsing logs and
files, so a throughput cliff at long context is precisely the wrong failure
mode. Mamba-2 layers carry a constant-size recurrent state instead of a growing
KV cache, which is what avoids that cliff.

Two gotchas that will bite an implementation
--------------------------------------------
1. **It is a reasoning model — disable thinking for cheap sub-steps.** Left on,
   it spends hundreds of reasoning tokens reformatting JSON, which inverts the
   entire point of routing mundane work to the cheap tier.
2. **The tool-call parser is ``qwen3_coder``, not plain OpenAI-style JSON.**
   Tool calls come back in that dialect and must be parsed accordingly; wiring
   an OpenAI JSON parser to it fails in ways that look like model incompetence
   rather than a parsing bug.

Where thinking control belongs
------------------------------
Not on :class:`~turing.research.backends.protocol.GenerationRequest` — see that
module on why reasoning control is absent from the neutral request. The natural
home is a per-tier flag on
:class:`~turing.research.backends.tiering.TieringPolicy`, translated by each
backend, so "sub-step tier thinks less" is one policy rather than a field only
one provider honours.

Implementing this
-----------------
Subclass :class:`~turing.research.backends.base.BaseBackend` and implement
``_invoke``. Accounting, checkpointing, and the resume-token engine check come
with it, so a local backend cannot accidentally count differently from the
cloud one. Report real token counts from the runtime where it exposes them; the
base class falls back to a conservative estimate where it does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turing.research.backends.base import BaseBackend, RawTurn
from turing.research.backends.errors import BackendNotImplementedError
from turing.research.backends.protocol import BackendIdentity

if TYPE_CHECKING:
    from turing.research.backends.protocol import GenerationRequest, GenerationResponse
    from turing.research.backends.tiering import TieringPolicy

BACKEND_NAME = "local"

#: The model this seam was researched against. Recorded here so the choice, and
#: the reasoning behind it, survive in the code rather than only in a brief.
RECOMMENDED_MODEL = "mlx-community/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-4bit"

#: Runtime the recommended weights were converted with, and which is installed.
RECOMMENDED_RUNTIME = "mlx-lm 0.31.3"

#: Tool-call dialect the recommended model emits. **Not** OpenAI-style JSON.
TOOL_CALL_PARSER = "qwen3_coder"

#: Inferred, never measured. See the module docstring.
EXPECTED_TOKENS_PER_SECOND = (80, 90)


class LocalBackend(BaseBackend):
    """Placeholder for a locally-hosted backend. Every call raises.

    Constructing one is allowed — it satisfies
    :class:`~turing.research.backends.protocol.ModelBackend`, so wiring can be
    type-checked against the seam before anything implements it. Calling
    :meth:`generate` raises
    :class:`~turing.research.backends.errors.BackendNotImplementedError`
    **without touching the ledger**: no call was made, so charging the cap for
    one would be an over-count in the one place where an over-count is not the
    safe direction — it would eat budget for work that never happened.
    """

    def __init__(self, *, policy: TieringPolicy | None = None) -> None:
        orchestrator = policy.orchestrator_model if policy else RECOMMENDED_MODEL
        substep = policy.substep_model if policy else RECOMMENDED_MODEL
        super().__init__(
            identity=BackendIdentity(
                backend=BACKEND_NAME,
                orchestrator_model=orchestrator,
                substep_model=substep,
            )
        )
        self._policy = policy

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Always raises. No local model is in the loop today."""
        raise BackendNotImplementedError(
            "the local backend is a documented seam, not an implementation; "
            f"see turing.research.backends.local for the research on {RECOMMENDED_MODEL} "
            f"({RECOMMENDED_RUNTIME}, tool parser {TOOL_CALL_PARSER!r}) and benchmark it "
            "on the target machine before wiring it in"
        )

    async def _invoke(self, request: GenerationRequest) -> RawTurn:
        """Unreachable — :meth:`generate` raises before delegating here."""
        raise BackendNotImplementedError("local backend has no provider call")
