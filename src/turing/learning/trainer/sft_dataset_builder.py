"""SFTDatasetBuilder — reasoning-bearing, accumulate-never-replace SFT corpus.

ADR 0009 §4 makes two of the fine-tuning guardrails load-bearing, and this
builder is where both live:

- **Always train on *accumulated* real + synthetic data; never replace.** Keep a
  frozen real seed set in every training run and only ever *add* the newly
  curated pairs each cycle. Under "replace", error grows unbounded; under
  "accumulate", it has a finite bound independent of iterations
  (arXiv:2404.01413, 2410.16713). The builder therefore owns a corpus that can
  only grow — :meth:`accumulate` adds, and there is deliberately **no** replace
  / clear method.
- **Capture reasoning / rationale, not just the final answer**, as the SFT
  target. Style-only imitation transfers fluency but not factuality; process
  distillation is what raises capability (Orca 2306.02707, Distilling
  Step-by-Step 2305.02301). Rows are therefore ``(question, reasoning, answer)``,
  sourced from the morning-curation :class:`SFTCandidate`.

The curated candidates come from
:class:`~turing.coordinator.flywheel.morning_curation.MorningCuration`; the
frozen seeds are the operator's real hand-written examples. The output is a
content-addressed JSONL the existing CUDA LoRA trainer consumes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from turing.coordinator.flywheel.morning_curation import SFTCandidate


@dataclass(frozen=True)
class SFTExample:
    """One reasoning-bearing training pair."""

    question: str
    reasoning: str
    answer: str
    specialty: str
    origin: str  # "seed" (frozen real) or "curated" (accumulated synthetic)

    def content_key(self) -> bytes:
        return hashlib.sha256(
            json.dumps(
                {"q": self.question, "r": self.reasoning, "a": self.answer},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).digest()

    def to_row(self) -> dict[str, str]:
        return {
            "question": self.question,
            "reasoning": self.reasoning,
            "answer": self.answer,
            "specialty": self.specialty,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class SFTDatasetInfo:
    out_path: Path
    example_count: int
    seed_count: int
    curated_count: int
    duplicates_dropped: int
    dataset_sha256: str


class DatasetReplaceError(RuntimeError):
    """Raised if a caller attempts to shrink the corpus (replace, not accumulate)."""


class SFTDatasetBuilder:
    """Accumulating, reasoning-bearing SFT corpus for one specialty.

    The frozen real **seed set** is added once at construction (or via
    :meth:`add_seeds`) and is included in *every* build. Each curation cycle
    calls :meth:`accumulate` to add the newly curated pairs. The corpus only
    grows; :meth:`build` writes the whole accumulated set, deduped, to JSONL.
    """

    def __init__(self, *, specialty: str, seeds: Iterable[SFTCandidate] = ()) -> None:
        self._specialty = specialty
        self._seeds: list[SFTExample] = []
        self._curated: list[SFTExample] = []
        self.add_seeds(seeds)

    def add_seeds(self, seeds: Iterable[SFTCandidate]) -> None:
        """Add frozen real seed examples (included in every training run)."""
        for cand in seeds:
            self._seeds.append(self._to_example(cand, origin="seed"))

    def accumulate(self, candidates: Iterable[SFTCandidate]) -> int:
        """Add newly curated pairs to the corpus. Returns the number added.

        This is the *only* way curated data enters the corpus — there is no
        replace/clear, enforcing the accumulate-never-replace guardrail.
        """
        before = len(self._curated)
        for cand in candidates:
            self._curated.append(self._to_example(cand, origin="curated"))
        return len(self._curated) - before

    @property
    def corpus_size(self) -> int:
        """Total examples (seed + curated) currently accumulated."""
        return len(self._seeds) + len(self._curated)

    def build(self, *, out_path: Path) -> SFTDatasetInfo:
        """Write the accumulated corpus (seed first, then curated) to JSONL.

        Byte-identical ``(question, reasoning, answer)`` triples are deduped;
        a curated pair that duplicates a frozen seed is dropped in favour of the
        seed. The dataset SHA256 is a content-addressed cache key for the trainer.
        """
        seen: set[bytes] = set()
        rows: list[dict[str, str]] = []
        duplicates = 0
        kept_seed = kept_curated = 0
        # Seeds first so a seed always wins a tie against a duplicate curated pair.
        for example in (*self._seeds, *self._curated):
            key = example.content_key()
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            rows.append(example.to_row())
            if example.origin == "seed":
                kept_seed += 1
            else:
                kept_curated += 1

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows)
        if rows:
            body += "\n"
        out_path.write_text(body, encoding="utf-8")

        return SFTDatasetInfo(
            out_path=out_path,
            example_count=len(rows),
            seed_count=kept_seed,
            curated_count=kept_curated,
            duplicates_dropped=duplicates,
            dataset_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )

    def assert_grew(self, *, previous_size: int) -> None:
        """Guard the accumulate-never-replace invariant across cycles.

        A later cycle's corpus must be ``>=`` an earlier one's. Raises
        :class:`DatasetReplaceError` if the corpus shrank — the failure mode the
        guardrail exists to prevent.
        """
        if self.corpus_size < previous_size:
            raise DatasetReplaceError(
                f"corpus shrank from {previous_size} to {self.corpus_size}: "
                "training data must accumulate, never replace (ADR 0009 §4)"
            )

    def _to_example(self, cand: SFTCandidate, *, origin: str) -> SFTExample:
        return SFTExample(
            question=cand.question,
            reasoning=cand.reasoning,
            answer=cand.answer,
            specialty=cand.specialty or self._specialty,
            origin=origin,
        )


def candidates_from_rows(rows: Sequence[dict[str, str]], *, specialty: str) -> list[SFTExample]:
    """Rehydrate :class:`SFTExample` rows written by :meth:`SFTDatasetBuilder.build`.

    Useful for an accumulating run that reloads the prior cycle's dataset as the
    frozen base before adding the new cycle's curated pairs.
    """
    return [
        SFTExample(
            question=r.get("question", ""),
            reasoning=r.get("reasoning", ""),
            answer=r.get("answer", ""),
            specialty=r.get("specialty", specialty),
            origin=r.get("origin", "seed"),
        )
        for r in rows
    ]
