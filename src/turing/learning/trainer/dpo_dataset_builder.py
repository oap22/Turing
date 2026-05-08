"""DPODatasetBuilder — episode store → (winner, loser) preference pairs.

Phase C trains on direct preferences. Pairs come from two natural sources:

1. **Critic-derived**: two episodes with the same ``(specialty, input_text)``
   but different critic scores. The high-scoring episode is the winner;
   the low-scoring one the loser. ``min_score_gap`` filters out pairs
   that are too close to be informative.
2. **Thumbs-derived**: subtask-thread thumbs from slice 17's reward
   attribution can produce explicit pairs once they accumulate. Those
   land via a follow-up that wires ``user_score`` onto the episode row;
   for now the builder uses critic scores only. The pair shape is
   identical, so the trainer doesn't care about the source.

Output is JSONL with ``{prompt, chosen, rejected}`` per line — the format
the trl/peft DPO trainer consumes directly. The dataset hash is
content-addressed (same input + outputs → same sha) for the
already-trained-this cache pattern from slice 24.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from turing.coordinator.lifecycle.lifecycle import SubtaskState

if TYPE_CHECKING:
    from pathlib import Path

    from turing.coordinator.lifecycle.episode_store import (
        Episode,
        EpisodeStore,
    )


@dataclass(frozen=True)
class DPODatasetInfo:
    out_path: Path
    pair_count: int
    dataset_sha256: str


class DPODatasetBuilder:
    def __init__(
        self,
        *,
        episode_store: EpisodeStore,
        min_score_gap: float = 0.3,
    ) -> None:
        self._store = episode_store
        self._min_gap = min_score_gap

    def build(
        self,
        *,
        specialty: str,
        out_path: Path,
    ) -> DPODatasetInfo:
        episodes = [
            ep
            for ep in self._store.query(
                specialty=specialty,
                min_score=0.0,
                positive_only=False,
            )
            if ep.outcome is SubtaskState.COMPLETED
        ]

        # Group by input — pairs only form between episodes that answered
        # the same prompt.
        by_input: dict[str, list[Episode]] = defaultdict(list)
        for ep in episodes:
            by_input[ep.input_text].append(ep)

        pairs: list[dict[str, str]] = []
        for input_text, group in by_input.items():
            if len(group) < 2:
                continue
            # Sort by critic score; pair best-vs-worst as long as gap clears
            # the threshold. We emit one pair per input to avoid drowning
            # the dataset in near-duplicates.
            group.sort(key=lambda e: e.critic_score, reverse=True)
            best, worst = group[0], group[-1]
            if best.critic_score - worst.critic_score < self._min_gap:
                continue
            pairs.append(
                {
                    "prompt": input_text,
                    "chosen": best.output_text,
                    "rejected": worst.output_text,
                }
            )

        # Stable order so the sha is content-addressed.
        pairs.sort(key=lambda p: p["prompt"])
        body = "\n".join(
            json.dumps(p, sort_keys=True, separators=(",", ":")) for p in pairs
        )
        if pairs:
            body += "\n"

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")
        return DPODatasetInfo(
            out_path=out_path,
            pair_count=len(pairs),
            dataset_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
