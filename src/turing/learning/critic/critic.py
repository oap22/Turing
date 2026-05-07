"""Critic protocol and CriticScore value object.

A critic produces a structured score for one episode. The protocol is async
because production critics call out to LLMs, but tests pass small fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from turing.coordinator.lifecycle.episode_store import Episode


@dataclass(frozen=True)
class CriticScore:
    correctness: float
    efficiency: float
    specialty_fit: float
    critique: str

    def __post_init__(self) -> None:
        for name, value in (
            ("correctness", self.correctness),
            ("efficiency", self.efficiency),
            ("specialty_fit", self.specialty_fit),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name}={value!r} must be in [0, 1]")

    @property
    def overall(self) -> float:
        return (self.correctness + self.efficiency + self.specialty_fit) / 3.0


class Critic(Protocol):
    async def score(self, episode: Episode) -> CriticScore: ...
