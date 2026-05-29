"""LoRA SFT recipe + round-cap guard (ADR 0009 §4).

Three of the fine-tuning guardrails are about *how* the LoRA is trained each
cycle, not what data it sees. This module encodes them as a validated recipe so
the trainer can't silently drift off the literature-backed settings:

- **Re-train the LoRA from the base model each cycle; do not stack adapters
  iteratively.** Iterative self-training overfits the small set and *regresses*
  (ReST-EM regressed on coding after iteration 1, arXiv:2312.06585). The recipe
  always starts from the pinned base — it cannot express adapter stacking, and
  ``train_from_base`` is required to be ``True``.
- **LoRA on *all* linear layers, modest rank, few epochs.** Adapter *placement*
  matters more than rank for matching full-FT quality, and few epochs guards
  small-data memorisation (QLoRA, arXiv:2305.14314).
- **Expect 1–3 useful rounds, then re-evaluate — not perpetual gains.** Every
  measured self-improvement loop saturates fast (ReST-EM; Self-Rewarding LMs
  2401.10020). :class:`RoundCapGuard` blocks a 4th round until a human
  re-evaluates.

The recipe is the single source of truth for a job's ``hyperparameters`` dict;
:meth:`LoraRecipe.to_hyperparameters` feeds the existing ``TrainingJob`` /
``CudaLoraTrainer`` path unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# QLoRA found adapter placement matters more than rank; "all linear layers"
# is the placement that matched full fine-tuning. For a Qwen2.5-class decoder
# that's the attention projections + the MLP projections.
ALL_LINEAR_TARGET_MODULES: tuple[str, ...] = (
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
)

# ADR 0009: 1-3 useful rounds, then re-evaluate. The cap is the *upper* bound;
# a human must re-evaluate before a further round.
MAX_USEFUL_ROUNDS = 3
# "Modest rank" + "few epochs" envelopes from QLoRA / the ADR discussion.
_MAX_RANK = 64
_MAX_EPOCHS = 4


class InvalidRecipeError(ValueError):
    """Raised when recipe settings violate an ADR 0009 §4 guardrail."""


class RoundCapExceededError(RuntimeError):
    """Raised when a cycle would exceed the 1-3-round cap without re-evaluation."""

    def __init__(self, *, round_number: int, cap: int) -> None:
        super().__init__(
            f"round {round_number} exceeds the {cap}-round cap; a human must "
            "re-evaluate before another self-improvement round (ADR 0009 §4)"
        )
        self.round_number = round_number
        self.cap = cap


@dataclass(frozen=True)
class LoraRecipe:
    """A validated LoRA SFT hyperparameter recipe honouring the §4 guardrails.

    Defaults sit inside the literature-backed envelope; construction validates
    every field so an out-of-envelope recipe fails fast rather than producing a
    silently-bad adapter. There is intentionally no ``resume``/``base_adapter``
    field — the recipe *cannot express* adapter stacking; every run is from base.
    """

    rank: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    learning_rate: float = 1e-4
    epochs: int = 2
    target_modules: tuple[str, ...] = ALL_LINEAR_TARGET_MODULES
    # Always-true marker that travels with the hyperparameters so a reader of a
    # job payload can see the from-base guarantee was asserted, not assumed.
    train_from_base: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.rank <= _MAX_RANK:
            raise InvalidRecipeError(f"rank must be in [1, {_MAX_RANK}], got {self.rank}")
        if not 1 <= self.epochs <= _MAX_EPOCHS:
            raise InvalidRecipeError(
                f"epochs must be in [1, {_MAX_EPOCHS}] (few-epochs guardrail), got {self.epochs}"
            )
        if self.lora_alpha <= 0:
            raise InvalidRecipeError(f"lora_alpha must be positive, got {self.lora_alpha}")
        if not 0.0 <= self.lora_dropout < 1.0:
            raise InvalidRecipeError(f"lora_dropout must be in [0, 1), got {self.lora_dropout}")
        if self.learning_rate <= 0.0:
            raise InvalidRecipeError(f"learning_rate must be positive, got {self.learning_rate}")
        if not self.target_modules:
            raise InvalidRecipeError("target_modules must be non-empty")
        if not self.train_from_base:
            raise InvalidRecipeError(
                "train_from_base must be True — adapters are retrained from base "
                "each cycle, never stacked (ADR 0009 §4)"
            )

    @property
    def targets_all_linear_layers(self) -> bool:
        """True when the recipe targets the full all-linear-layer set."""
        return set(self.target_modules) >= set(ALL_LINEAR_TARGET_MODULES)

    def to_hyperparameters(self) -> dict[str, Any]:
        """Render the recipe as a ``TrainingJob.hyperparameters`` dict."""
        return {
            "rank": self.rank,
            "lora_alpha": self.lora_alpha,
            "lora_dropout": self.lora_dropout,
            "learning_rate": self.learning_rate,
            "epochs": self.epochs,
            "target_modules": list(self.target_modules),
            "train_from_base": self.train_from_base,
        }

    @classmethod
    def from_hyperparameters(cls, hp: dict[str, Any]) -> LoraRecipe:
        """Parse + validate a hyperparameters dict back into a recipe.

        Unknown keys are ignored; missing keys fall back to the safe defaults.
        Validation still runs, so a tampered payload (e.g. ``epochs: 50``) is
        rejected rather than trained.
        """
        tm = hp.get("target_modules")
        return cls(
            rank=int(hp.get("rank", 16)),
            lora_alpha=int(hp.get("lora_alpha", 32)),
            lora_dropout=float(hp.get("lora_dropout", 0.05)),
            learning_rate=float(hp.get("learning_rate", 1e-4)),
            epochs=int(hp.get("epochs", 2)),
            target_modules=tuple(tm) if tm else ALL_LINEAR_TARGET_MODULES,
            train_from_base=bool(hp.get("train_from_base", True)),
        )


@dataclass
class RoundCapGuard:
    """Enforces the 1-3-useful-rounds-then-re-evaluate cap per specialty.

    A cycle calls :meth:`check_and_increment` before training. Rounds 1-3 pass;
    a 4th raises :class:`RoundCapExceededError` until :meth:`reset` is called
    (the human re-evaluation that unlocks a fresh cap window).
    """

    cap: int = MAX_USEFUL_ROUNDS
    _rounds: dict[str, int] = field(default_factory=dict)

    def rounds_used(self, specialty: str) -> int:
        return self._rounds.get(specialty, 0)

    def check_and_increment(self, specialty: str) -> int:
        """Reserve the next round for ``specialty``; return its 1-based number.

        Raises :class:`RoundCapExceededError` if the cap is already reached.
        """
        used = self._rounds.get(specialty, 0)
        if used >= self.cap:
            raise RoundCapExceededError(round_number=used + 1, cap=self.cap)
        self._rounds[specialty] = used + 1
        return used + 1

    def reset(self, specialty: str) -> None:
        """Re-open the cap window after a human re-evaluation."""
        self._rounds.pop(specialty, None)
