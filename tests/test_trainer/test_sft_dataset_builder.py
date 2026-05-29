"""Tests for SFTDatasetBuilder (ADR 0009 §4: accumulate-never-replace +
reasoning-bearing SFT targets).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from turing.coordinator.flywheel.morning_curation import SFTCandidate
from turing.learning.trainer import (
    DatasetReplaceError,
    SFTDatasetBuilder,
    candidates_from_rows,
)

if TYPE_CHECKING:
    from pathlib import Path

SPECIALTY = "ai-ml-generalist"


def _cand(q: str, r: str, a: str, specialty: str = SPECIALTY) -> SFTCandidate:
    return SFTCandidate(question=q, reasoning=r, answer=a, specialty=specialty)


def _read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_rows_are_reasoning_bearing(tmp_path: Path) -> None:
    builder = SFTDatasetBuilder(specialty=SPECIALTY)
    builder.accumulate(
        [_cand("What is LoRA?", "Low-rank decomposition of weight deltas.", "LoRA adds A·B.")]
    )

    info = builder.build(out_path=tmp_path / "ds.jsonl")
    rows = _read_rows(info.out_path)

    assert len(rows) == 1
    assert rows[0]["question"] == "What is LoRA?"
    assert rows[0]["reasoning"] == "Low-rank decomposition of weight deltas."
    assert rows[0]["answer"] == "LoRA adds A·B."
    # The target carries reasoning, not just the answer.
    assert rows[0]["reasoning"] and rows[0]["reasoning"] != rows[0]["answer"]


def test_seeds_included_in_every_build(tmp_path: Path) -> None:
    seeds = [_cand("seed q", "seed reasoning", "seed answer")]
    builder = SFTDatasetBuilder(specialty=SPECIALTY, seeds=seeds)
    builder.accumulate([_cand("curated q", "curated reasoning", "curated answer")])

    info = builder.build(out_path=tmp_path / "ds.jsonl")

    assert info.seed_count == 1
    assert info.curated_count == 1
    origins = {r["origin"] for r in _read_rows(info.out_path)}
    assert origins == {"seed", "curated"}


def test_accumulate_only_grows_the_corpus(tmp_path: Path) -> None:
    builder = SFTDatasetBuilder(specialty=SPECIALTY, seeds=[_cand("s", "sr", "sa")])
    size0 = builder.corpus_size

    n1 = builder.accumulate([_cand("q1", "r1", "a1")])
    size1 = builder.corpus_size
    n2 = builder.accumulate([_cand("q2", "r2", "a2"), _cand("q3", "r3", "a3")])
    size2 = builder.corpus_size

    assert n1 == 1 and n2 == 2
    assert size0 < size1 < size2
    # The builder offers no replace/clear method — the API itself enforces it.
    assert not hasattr(builder, "replace")
    assert not hasattr(builder, "clear")


def test_assert_grew_raises_when_corpus_shrinks() -> None:
    builder = SFTDatasetBuilder(specialty=SPECIALTY)
    builder.accumulate([_cand("q", "r", "a")])
    # Simulate a prior cycle that was larger (the replace failure mode).
    with pytest.raises(DatasetReplaceError):
        builder.assert_grew(previous_size=5)
    # A non-shrinking cycle is fine.
    builder.assert_grew(previous_size=1)


def test_duplicate_curated_pair_deduped_seed_wins(tmp_path: Path) -> None:
    shared = _cand("dup q", "dup r", "dup a")
    builder = SFTDatasetBuilder(specialty=SPECIALTY, seeds=[shared])
    builder.accumulate([shared, _cand("unique q", "unique r", "unique a")])

    info = builder.build(out_path=tmp_path / "ds.jsonl")

    assert info.example_count == 2  # the dup collapsed to one
    assert info.duplicates_dropped == 1
    # The surviving copy of the duplicate is the seed, not the curated one.
    rows = _read_rows(info.out_path)
    dup_row = next(r for r in rows if r["question"] == "dup q")
    assert dup_row["origin"] == "seed"


def test_dataset_sha_is_content_addressed(tmp_path: Path) -> None:
    cands = [_cand("q", "r", "a")]
    b1 = SFTDatasetBuilder(specialty=SPECIALTY)
    b1.accumulate(cands)
    b2 = SFTDatasetBuilder(specialty=SPECIALTY)
    b2.accumulate(cands)

    i1 = b1.build(out_path=tmp_path / "a.jsonl")
    i2 = b2.build(out_path=tmp_path / "b.jsonl")
    assert i1.dataset_sha256 == i2.dataset_sha256  # same content → same key


def test_round_trip_via_candidates_from_rows(tmp_path: Path) -> None:
    """A later cycle can reload the prior dataset as its frozen base."""
    builder = SFTDatasetBuilder(specialty=SPECIALTY)
    builder.accumulate([_cand("q", "r", "a")])
    info = builder.build(out_path=tmp_path / "cycle1.jsonl")

    reloaded = candidates_from_rows(_read_rows(info.out_path), specialty=SPECIALTY)
    assert len(reloaded) == 1
    assert reloaded[0].question == "q"
    assert reloaded[0].reasoning == "r"
