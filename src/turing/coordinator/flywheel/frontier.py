"""Question frontier: Evol-Instruct expansion + elimination + dedup (issue #266).

This is the one pipeline stage that grows the question frontier **without
combinatorial drift**, run over proposed/seed questions *before* any of them
can enter the runnable queue. Two literature-backed guardrails (ADR 0009 §4):

- **Evol-Instruct expansion + explicit elimination** (WizardLM, arXiv:2304.12244):
  expand each question along an **in-depth** (harder/more specific) and an
  **in-breadth** (new but related) axis, then run an **explicit elimination
  step** that discards low-information, copied, or unanswerable evolutions. The
  discard step is the mechanism, not an afterthought — every drop logs a reason.
- **Dedup gate** (Self-Instruct, arXiv:2212.10560): reject a candidate whose
  ROUGE-L similarity to anything already on the frontier is ≥ 0.7, before it
  enters the queue.

Only the survivors of both gates are eligible to enter the runnable queue. The
**human approval** that actually promotes them (Frontier control) and any
*autonomous* expansion-without-caps are explicitly out of scope here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = structlog.get_logger(__name__)

# Self-Instruct's ROUGE-L gate: a candidate too similar to the existing frontier
# is dropped as redundant.
ROUGE_L_DEDUP_THRESHOLD = 0.7
# A near-identical evolution (vs its own origin) is a "copied" elimination.
COPIED_THRESHOLD = 0.95
# Below this many distinct content tokens a question is "low information".
MIN_INFORMATION_TOKENS = 4


class EvolutionAxis(StrEnum):
    IN_DEPTH = "in_depth"  # harder / more specific
    IN_BREADTH = "in_breadth"  # new but related


@dataclass(frozen=True)
class EvolvedQuestion:
    """A question produced by expanding a seed along one axis."""

    prompt: str
    axis: EvolutionAxis
    origin: str  # the seed prompt this grew from


@dataclass(frozen=True)
class FrontierResult:
    """Outcome of running the frontier pipeline over a set of seeds."""

    admitted: tuple[EvolvedQuestion, ...] = ()
    eliminated: tuple[tuple[EvolvedQuestion, str], ...] = ()  # (question, reason)
    deduped: tuple[tuple[EvolvedQuestion, str], ...] = ()  # (question, reason)


class Evolver(Protocol):
    """Produces Evol-Instruct variants of a question along one axis (LLM-backed
    in production; injected so the pipeline is testable without an LLM)."""

    async def evolve(self, *, question: str, axis: EvolutionAxis) -> list[str]: ...


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _lcs_len(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        curr = [0] * (len(b) + 1)
        for j, y in enumerate(b, start=1):
            curr[j] = prev[j - 1] + 1 if x == y else max(prev[j], curr[j - 1])
        prev = curr
    return prev[-1]


def rouge_l(a: str, b: str) -> float:
    """ROUGE-L F-measure over word tokens (0.0–1.0)."""
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    lcs = _lcs_len(ta, tb)
    if lcs == 0:
        return 0.0
    precision = lcs / len(tb)
    recall = lcs / len(ta)
    return 2 * precision * recall / (precision + recall)


def default_eliminator(question: EvolvedQuestion) -> str | None:
    """Heuristic elimination of low-information / copied / unanswerable evolutions."""
    prompt = question.prompt.strip()
    if not prompt:
        return "unanswerable: empty prompt"
    tokens = set(_tokenize(prompt))
    if len(tokens) < MIN_INFORMATION_TOKENS:
        return "low_information: too few distinct content tokens"
    if rouge_l(prompt, question.origin) >= COPIED_THRESHOLD:
        return "copied: near-identical to the origin question"
    return None


class QuestionFrontier:
    """Expand → eliminate → dedup, yielding queue-eligible survivors."""

    def __init__(
        self,
        *,
        evolver: Evolver,
        eliminator: Callable[[EvolvedQuestion], str | None] = default_eliminator,
        rouge_threshold: float = ROUGE_L_DEDUP_THRESHOLD,
    ) -> None:
        self._evolver = evolver
        self._eliminator = eliminator
        self._rouge_threshold = rouge_threshold

    async def expand(self, seed: str) -> list[EvolvedQuestion]:
        """Produce in-depth **and** in-breadth variants of ``seed``."""
        out: list[EvolvedQuestion] = []
        for axis in (EvolutionAxis.IN_DEPTH, EvolutionAxis.IN_BREADTH):
            for variant in await self._evolver.evolve(question=seed, axis=axis):
                if variant.strip():
                    out.append(EvolvedQuestion(prompt=variant.strip(), axis=axis, origin=seed))
        return out

    def eliminate(
        self, candidates: Iterable[EvolvedQuestion]
    ) -> tuple[list[EvolvedQuestion], list[tuple[EvolvedQuestion, str]]]:
        """Run the explicit elimination step. Returns ``(survivors, dropped)``."""
        survivors: list[EvolvedQuestion] = []
        dropped: list[tuple[EvolvedQuestion, str]] = []
        for q in candidates:
            reason = self._eliminator(q)
            if reason is None:
                survivors.append(q)
            else:
                dropped.append((q, reason))
                logger.info("frontier_eliminated", prompt=q.prompt, axis=q.axis, reason=reason)
        return survivors, dropped

    def dedup(
        self, survivors: Iterable[EvolvedQuestion], *, existing: Iterable[str]
    ) -> tuple[list[EvolvedQuestion], list[tuple[EvolvedQuestion, str]]]:
        """Reject near-duplicates (ROUGE-L ≥ threshold) vs the existing frontier
        and vs already-admitted survivors. Returns ``(admitted, rejected)``."""
        seen = list(existing)
        admitted: list[EvolvedQuestion] = []
        rejected: list[tuple[EvolvedQuestion, str]] = []
        for q in survivors:
            best = max((rouge_l(q.prompt, prior) for prior in seen), default=0.0)
            if best >= self._rouge_threshold:
                reason = f"near_duplicate: ROUGE-L {best:.2f} ≥ {self._rouge_threshold:.2f}"
                rejected.append((q, reason))
                logger.info("frontier_deduped", prompt=q.prompt, rouge_l=round(best, 3))
            else:
                admitted.append(q)
                seen.append(q.prompt)
        return admitted, rejected

    async def process(
        self, *, seeds: Iterable[str], existing_frontier: Iterable[str] = ()
    ) -> FrontierResult:
        """Full pipeline: expand every seed, eliminate, then dedup-gate."""
        expanded: list[EvolvedQuestion] = []
        for seed in seeds:
            expanded.extend(await self.expand(seed))
        survivors, dropped = self.eliminate(expanded)
        admitted, rejected = self.dedup(survivors, existing=existing_frontier)
        return FrontierResult(
            admitted=tuple(admitted),
            eliminated=tuple(dropped),
            deduped=tuple(rejected),
        )
