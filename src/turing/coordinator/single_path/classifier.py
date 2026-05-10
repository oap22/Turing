"""Specialty classifiers for the single-path Discord route.

Two implementations live behind a shared ``SpecialtyChoice`` return shape:

- ``KeywordSpecialtyClassifier`` — deterministic keyword/substring match.
  Cheap, predictable, perfect for tests and the dev loop.
- ``LLMSpecialtyClassifier`` — LLM-backed; falls back to a default when the
  model returns a label outside the allowed set, so a hallucinated specialty
  cannot route a task to a worker that doesn't exist.

Both expose a ``reason`` so the structured-log line on dispatch is
informative — a key bit of the issue's auditability requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from turing.llm.base import Message, Role

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


@dataclass(frozen=True)
class SpecialtyChoice:
    specialty: str
    reason: str


class SpecialtyClassifier(Protocol):
    def classify(self, message: str) -> SpecialtyChoice: ...


class KeywordSpecialtyClassifier:
    def __init__(
        self,
        *,
        mapping: Mapping[str, Iterable[str]],
        default: str,
    ) -> None:
        self._mapping = {k: tuple(v) for k, v in mapping.items()}
        self._default = default

    def classify(self, message: str) -> SpecialtyChoice:
        lowered = message.lower()
        for specialty, keywords in self._mapping.items():
            for kw in keywords:
                if kw.lower() in lowered:
                    return SpecialtyChoice(
                        specialty=specialty,
                        reason=f"matched keyword {kw!r}",
                    )
        return SpecialtyChoice(
            specialty=self._default,
            reason="no keyword matched, using default",
        )


class _LLMLike(Protocol):
    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        **_: object,
    ) -> object: ...


class LLMSpecialtyClassifier:
    """Async classifier backed by the LLM router.

    Returns the model's choice if it appears in ``allowed`` (case-insensitive,
    stripped); falls back to ``default`` otherwise. The reason field carries
    the raw model output so an off-policy answer is captured in the audit
    log without coercing the routing decision.
    """

    def __init__(
        self,
        *,
        llm: _LLMLike,
        allowed: tuple[str, ...],
        default: str,
    ) -> None:
        self._llm = llm
        self._allowed = tuple(allowed)
        self._default = default

    async def classify(self, message: str) -> SpecialtyChoice:
        system = (
            "Choose ONE specialty for the user request. Reply with just the "
            f"specialty name. Allowed: {', '.join(self._allowed)}."
        )
        response = await self._llm.complete(
            messages=[Message(role=Role.USER, content=message)],
            system=system,
        )
        raw = getattr(response, "content", "").strip()
        normalized = raw.lower()
        for s in self._allowed:
            if s.lower() == normalized:
                return SpecialtyChoice(specialty=s, reason=f"llm chose {raw!r}")
        return SpecialtyChoice(
            specialty=self._default,
            reason=f"llm response {raw!r} not in allowed set; using default",
        )
