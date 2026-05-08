"""TrainingJobBuilder — episode store query → dataset JSONL with content-addressed dedup.

The builder is the bridge between the closed-episode corpus and a training
run. It picks the top-K positive episodes for a specialty (filtered by
critic score and outcome), drops byte-identical (input, output) duplicates,
writes one line per example to a JSONL file, and reports a SHA-256 digest
over the dataset. The digest lets the trainer skip a run when the same
dataset has already been used — a content-addressed cache key, not a
timestamp or run id.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from turing.coordinator.lifecycle.lifecycle import SubtaskState

if TYPE_CHECKING:
    from turing.coordinator.lifecycle.episode_store import EpisodeStore


@dataclass(frozen=True)
class DatasetInfo:
    out_path: Path
    example_count: int
    duplicates_dropped: int
    dataset_sha256: str


class TrainingJobBuilder:
    def __init__(
        self,
        *,
        episode_store: EpisodeStore,
        min_critic_score: float = 0.7,
    ) -> None:
        self._store = episode_store
        self._min_score = min_critic_score

    def build(
        self,
        *,
        specialty: str,
        top_k: int,
        out_path: Path,
    ) -> DatasetInfo:
        candidates = self._store.query(
            specialty=specialty,
            min_score=self._min_score,
            positive_only=True,
            limit=top_k,
        )
        # Belt-and-braces: positive_only filters REJECTED but Phase B treats
        # any non-COMPLETED outcome as off-corpus (FAILED especially —
        # those land via archive_failed_training and must not be promoted
        # back into positive training data).
        kept = [e for e in candidates if e.outcome is SubtaskState.COMPLETED]

        seen_keys: set[bytes] = set()
        rows: list[dict[str, str]] = []
        duplicates = 0
        for ep in kept:
            row = {"input": ep.input_text, "output": ep.output_text}
            digest = hashlib.sha256(
                json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).digest()
            if digest in seen_keys:
                duplicates += 1
                continue
            seen_keys.add(digest)
            rows.append(row)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(
            json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows
        )
        if rows:
            body += "\n"
        out_path.write_text(body, encoding="utf-8")

        return DatasetInfo(
            out_path=out_path,
            example_count=len(rows),
            duplicates_dropped=duplicates,
            dataset_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
