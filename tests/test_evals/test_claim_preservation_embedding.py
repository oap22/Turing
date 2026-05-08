"""Tests for embedding-similarity claim presence (issue #87).

The v1 scorer used substring + token-overlap, which fails on paraphrases.
v2 accepts an injected embedder; when present, a claim counts as found if
cosine(claim, any_summary_sentence) >= threshold (default 0.75). The
substring fast path is retained for exact matches so tests without an
embedder still work.
"""

from __future__ import annotations

import math
import re
from typing import Protocol

import pytest

from turing.evals.research_summarize.harness import load_cases, run
from turing.evals.research_summarize.schema import EvalCase, Expected, SourceDoc
from turing.evals.research_summarize.scoring import (
    score_claim_preservation_v1,
)


class _Embedder(Protocol):
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class CannedEmbedder:
    """Mock embedder. Returns a unit vector per registered text, default
    orthogonal so unregistered texts have ~0 cosine to everything."""

    def __init__(self, registry: dict[str, list[float]]):
        self._registry = {self._norm(k): self._normalize(v) for k, v in registry.items()}

    @staticmethod
    def _norm(s: str) -> str:
        return s.strip().lower()

    @staticmethod
    def _normalize(v: list[float]) -> list[float]:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            key = self._norm(t)
            if key in self._registry:
                out.append(self._registry[key])
            else:
                # Unique orthogonal-ish vector per unregistered text so
                # unrelated content has near-zero cosine to anything we care about.
                seed = abs(hash(key)) % 997
                vec = [0.0] * 1000
                vec[seed] = 1.0
                out.append(vec)
        return out


def _make_case(
    *,
    must_contain: list[str],
    must_not: list[str] | None = None,
    threshold: float | None = None,
    summary_id: str = "test",
) -> EvalCase:
    expected = Expected(
        must_contain_claims=must_contain,
        must_not_contain=must_not or [],
    )
    if threshold is not None:
        expected.claim_match_threshold = threshold  # type: ignore[attr-defined]
    return EvalCase(
        id=summary_id,
        category="claim_preservation",
        prompt="test",
        source_docs=[SourceDoc(id="src_1", text="ignored")],
        expected=expected,
        scoring_fn="score_claim_preservation_v1",
    )


# ─── Substring fast path (no embedder) ─────────────────────────────────────


def test_exact_substring_matches_without_embedder():
    case = _make_case(must_contain=["foo bar baz"])
    result = score_claim_preservation_v1("the foo bar baz appears here", case)
    assert result.score == 1.0


def test_token_overlap_fallback_without_embedder():
    case = _make_case(must_contain=["model size reduced 73.8%"])
    # Same content tokens, reordered: token-overlap fallback should still hit.
    summary = "INT4 reduced the model size by 73.8% across all layers."
    result = score_claim_preservation_v1(summary, case)
    assert result.score == 1.0


# ─── Embedder path: paraphrase robustness ──────────────────────────────────


def test_paraphrase_matched_via_embedder_above_threshold():
    """v1 misses paraphrases; v2 with embedder catches them."""
    claim = "slow-wave sleep correlates with declarative memory recall"
    paraphrase = "deep sleep stages predict how well facts are remembered"

    # Both vectors close (cosine ~0.92): aligned in a shared 2-D direction.
    embedder = CannedEmbedder(
        {
            claim: [1.0, 0.0],
            paraphrase: [0.92, math.sqrt(1 - 0.92**2)],
        }
    )

    case = _make_case(must_contain=[claim])
    result = score_claim_preservation_v1(paraphrase, case, embedder=embedder)
    assert result.score == 1.0, result.notes


def test_unrelated_content_below_threshold_does_not_match():
    claim = "weight loss comparable to continuous calorie restriction"
    unrelated = "the cathode showed no dendrite formation after 1000 cycles"

    embedder = CannedEmbedder(
        {
            claim: [1.0, 0.0],
            unrelated: [0.2, math.sqrt(1 - 0.2**2)],  # cosine ~0.2
        }
    )

    case = _make_case(must_contain=[claim])
    result = score_claim_preservation_v1(unrelated, case, embedder=embedder)
    assert result.score == 0.0


def test_per_case_threshold_override_blocks_borderline_match():
    claim = "lower loss per FLOP than dense"
    near_paraphrase = "smaller loss per compute unit than dense models"

    # cosine 0.80 — passes default 0.75 but should fail an override of 0.90.
    embedder = CannedEmbedder(
        {
            claim: [1.0, 0.0],
            near_paraphrase: [0.80, math.sqrt(1 - 0.80**2)],
        }
    )

    strict_case = _make_case(must_contain=[claim], threshold=0.90)
    strict = score_claim_preservation_v1(near_paraphrase, strict_case, embedder=embedder)
    assert strict.score == 0.0

    default_case = _make_case(must_contain=[claim])
    default = score_claim_preservation_v1(near_paraphrase, default_case, embedder=embedder)
    assert default.score == 1.0


def test_substring_fast_path_skips_embedder_call():
    """Exact substring should hit before the embedder is consulted."""
    calls = {"n": 0}

    class CountingEmbedder:
        def embed_batch(self, texts):
            calls["n"] += 1
            return [[0.0] * 384 for _ in texts]

    case = _make_case(must_contain=["the exact phrase"])
    result = score_claim_preservation_v1(
        "the exact phrase appears here verbatim", case, embedder=CountingEmbedder()
    )
    assert result.score == 1.0
    assert calls["n"] == 0


# ─── Discrimination preserved across the full case set ─────────────────────


def test_full_case_set_discrimination_preserved_with_embedder():
    """All 12 claim_preservation cases: good worker 1.0, fluff worker < 0.2.
    Holds with embedder injected (mock returns near-perfect for matching
    text, near-zero for unrelated)."""

    def good(case: EvalCase) -> str:
        return " ".join(case.expected.must_contain_claims)

    def fluff(case: EvalCase) -> str:
        return (
            "Moreover, this paper delves into a robust framework that leverages "
            "cutting-edge methods. It is worth noting the powerful results."
        )

    class LexicalEmbedder:
        """Bag-of-content-words embedder.

        Cosine ≈ Jaccard-ish over content tokens. Realistic enough to drive
        the scorer's embedding path: paraphrases of the claim score high,
        unrelated fluff scores ~0.
        """

        def embed_batch(self, texts):
            # Build vocabulary across this batch only.
            vocab: dict[str, int] = {}
            tokenized = []
            for t in texts:
                toks = [w for w in re.findall(r"\w+", t.lower()) if len(w) > 3]
                for w in toks:
                    vocab.setdefault(w, len(vocab))
                tokenized.append(toks)
            dim = max(1, len(vocab))
            vecs = []
            for toks in tokenized:
                v = [0.0] * dim
                for w in toks:
                    v[vocab[w]] += 1.0
                vecs.append(v)
            return vecs

    cases = [c for c in load_cases() if c.category == "claim_preservation"]
    assert len(cases) == 12

    good_scores = [
        score_claim_preservation_v1(good(c), c, embedder=LexicalEmbedder()).score for c in cases
    ]
    fluff_scores = [
        score_claim_preservation_v1(fluff(c), c, embedder=LexicalEmbedder()).score for c in cases
    ]

    assert all(s == 1.0 for s in good_scores), good_scores
    assert sum(fluff_scores) / len(fluff_scores) < 0.2


def test_harness_runs_without_embedder_using_existing_fallback():
    """Backwards compat: harness with no embedder still works."""
    from turing.evals.research_summarize.harness import fixture_worker

    cases = load_cases()
    report = run(fixture_worker, cases)
    assert report["by_category"]["claim_preservation"] == pytest.approx(1.0)
