"""TrainingJobProposal — what gets shown to the operator on Discord."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ALLOWED_METHODS = ("sft", "dpo")


@dataclass(frozen=True)
class TrainingJobProposal:
    specialty: str
    base_model: str
    method: Literal["sft", "dpo"]
    estimated_cost_usd: float
    dataset_summary: str

    def __post_init__(self) -> None:
        if self.method not in ALLOWED_METHODS:
            raise ValueError(f"method must be one of {ALLOWED_METHODS}, got {self.method!r}")
