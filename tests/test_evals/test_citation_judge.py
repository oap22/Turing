"""Tests for citation-correctness scoring v2 (issue #88).

v1 only checked that each required src_id appeared as a `[[src_id]]`
wiki-link somewhere in the summary. v2 grades sentence-level proximity
against a Haiku judge that classifies each (claim, source_text) pair as
supports / unrelated / contradicts. Final score is F1 of precision and
recall.
"""

from __future__ import annotations

import pytest

from turing.evals.research_summarize.harness import run
from turing.evals.research_summarize.judge import (
    CachedJudge,
    Judge,
    JudgeVerdict,
)
from turing.evals.research_summarize.schema import EvalCase, Expected, SourceDoc
from turing.evals.research_summarize.scoring import (
    score_citation_correctness_v1,
)


class FakeJudge:
    """Deterministic judge keyed by (claim_substring, source_id) -> verdict.

    Lets each test stipulate exactly what the judge would say without
    spinning up a real model.
    """

    def __init__(self, verdicts: dict[tuple[str, str], JudgeVerdict]):
        self._verdicts = verdicts
        self.calls: list[tuple[str, str]] = []

    def judge(self, *, claim: str, source_id: str, source_text: str) -> JudgeVerdict:
        self.calls.append((claim, source_id))
        for (claim_key, sid_key), verdict in self._verdicts.items():
            if claim_key in claim and sid_key == source_id:
                return verdict
        return "unrelated"


def _case(
    *,
    src_text: str = "INT4 quantization compresses model size 4x.",
    required: dict[str, list[str]] | None = None,
) -> EvalCase:
    return EvalCase(
        id="cite-test",
        category="citation_correctness",
        prompt="Summarize.",
        source_docs=[SourceDoc(id="src_1", text=src_text)],
        expected=Expected(
            required_citations=required
            if required is not None
            else {"src_1": ["INT4 quantization compresses model size 4x"]}
        ),
        scoring_fn="score_citation_correctness_v1",
    )


# ─── Sentence-level proximity ──────────────────────────────────────────────


def test_citation_in_same_sentence_as_claim_counts():
    judge = FakeJudge({("INT4 quantization", "src_1"): "supports"})
    case = _case()
    summary = "INT4 quantization compresses model size 4x [[src_1]]."
    result = score_citation_correctness_v1(summary, case, judge=judge)
    assert result.score == 1.0


def test_citation_in_different_sentence_does_not_ground_claim():
    judge = FakeJudge({("INT4 quantization", "src_1"): "supports"})
    case = _case()
    # Claim and citation in different sentences -> claim is ungrounded.
    summary = "INT4 quantization compresses model size 4x. See [[src_1]] for details."
    result = score_citation_correctness_v1(summary, case, judge=judge)
    # Recall should drop (the required claim has no proximity-grounded citation).
    assert result.breakdown["recall"] < 1.0


# ─── Judge verdicts drive precision and recall ─────────────────────────────


def test_judge_unrelated_verdict_does_not_ground_claim():
    judge = FakeJudge({("INT4", "src_1"): "unrelated"})
    case = _case()
    summary = "INT4 quantization compresses model size 4x [[src_1]]."
    result = score_citation_correctness_v1(summary, case, judge=judge)
    # Citation present but judge says unrelated -> claim ungrounded AND
    # precision drops because the citation made was unsupported.
    assert result.breakdown["recall"] == 0.0
    assert result.breakdown["precision"] == 0.0


def test_f1_combines_precision_and_recall():
    """Two required pairs; one grounded correctly, one cited but judge says
    unrelated. Recall=0.5, precision=0.5, F1=0.5."""
    judge = FakeJudge(
        {
            ("INT4 quantization", "src_1"): "supports",
            ("FlashAttention", "src_2"): "unrelated",
        }
    )
    case = EvalCase(
        id="multi",
        category="citation_correctness",
        prompt="Summarize.",
        source_docs=[
            SourceDoc(id="src_1", text="INT4 compresses 4x."),
            SourceDoc(id="src_2", text="FlashAttention is exact."),
        ],
        expected=Expected(
            required_citations={
                "src_1": ["INT4 quantization compresses 4x"],
                "src_2": ["FlashAttention is mathematically exact"],
            }
        ),
        scoring_fn="score_citation_correctness_v1",
    )
    summary = (
        "INT4 quantization compresses 4x [[src_1]]. "
        "FlashAttention is mathematically exact [[src_2]]."
    )
    result = score_citation_correctness_v1(summary, case, judge=judge)
    assert result.breakdown["recall"] == 0.5
    assert result.breakdown["precision"] == 0.5
    assert result.score == pytest.approx(0.5)


# ─── Backwards compatibility: no judge -> v1 presence-only behaviour ───────


def test_no_judge_falls_back_to_presence_check():
    """v1 behaviour: just check that each required src_id appears as a
    [[src_id]] wiki-link. Used when the harness runs without judge wired up."""
    case = _case()
    summary = "Some summary text [[src_1]] is referenced."
    result = score_citation_correctness_v1(summary, case)
    assert result.score == 1.0


def test_no_judge_missing_citation_drops_recall():
    case = _case()
    summary = "Summary with no citation at all."
    result = score_citation_correctness_v1(summary, case)
    assert result.score < 1.0


# ─── Caching: identical pairs hit the underlying judge once ────────────────


def test_cached_judge_calls_underlying_once_per_unique_pair(tmp_path):
    inner = FakeJudge({("X", "src_1"): "supports"})
    cache_path = tmp_path / "cache.json"
    cached: Judge = CachedJudge(inner=inner, cache_path=cache_path)

    cached.judge(claim="X is true", source_id="src_1", source_text="X")
    cached.judge(claim="X is true", source_id="src_1", source_text="X")
    cached.judge(claim="X is true", source_id="src_1", source_text="X")

    # First call hits inner; next two are cached.
    assert len(inner.calls) == 1


def test_cached_judge_persists_across_instances(tmp_path):
    inner1 = FakeJudge({("X", "src_1"): "supports"})
    cache_path = tmp_path / "cache.json"
    CachedJudge(inner=inner1, cache_path=cache_path).judge(
        claim="X is true", source_id="src_1", source_text="X"
    )
    assert len(inner1.calls) == 1

    # Fresh process: new inner judge should never be called.
    inner2 = FakeJudge({("X", "src_1"): "supports"})
    verdict = CachedJudge(inner=inner2, cache_path=cache_path).judge(
        claim="X is true", source_id="src_1", source_text="X"
    )
    assert verdict == "supports"
    assert len(inner2.calls) == 0


# ─── Harness threads judge through to citation scorer ──────────────────────


def test_harness_threads_judge_to_citation_scorer():
    judge = FakeJudge({("INT4", "src_1"): "supports"})
    case = _case()
    case.id = "cite-via-harness"

    def worker(_c):
        return "INT4 quantization compresses model size 4x [[src_1]]."

    report = run([case].__iter__().__next__ if False else worker, [case], judge=judge)
    assert report["per_case"][0]["score"] == 1.0


# ─── Discrimination across the full citation case set ─────────────────────


def test_full_citation_case_set_discriminates_with_judge():
    """A worker that cites correctly with judge='supports' for matching
    claims scores high; a worker that doesn't cite scores low."""
    from turing.evals.research_summarize.harness import load_cases

    cases = [c for c in load_cases() if c.category == "citation_correctness"]
    assert len(cases) == 10

    # Permissive judge: anything cited "supports" for the matching source.
    class PermissiveJudge:
        def judge(self, *, claim, source_id, source_text):
            return "supports"

    def good_worker(case: EvalCase) -> str:
        # For each required source, write the claim text inline followed by
        # the wiki-link in the same sentence.
        sentences = []
        for src_id, claims in case.expected.required_citations.items():
            for claim in claims:
                sentences.append(f"{claim} [[{src_id}]].")
        return " ".join(sentences) or "(empty)"

    def bad_worker(case: EvalCase) -> str:
        return "Generic summary with no citations and no real claims."

    good = sum(
        score_citation_correctness_v1(good_worker(c), c, judge=PermissiveJudge()).score
        for c in cases
    ) / len(cases)
    bad = sum(
        score_citation_correctness_v1(bad_worker(c), c, judge=PermissiveJudge()).score
        for c in cases
    ) / len(cases)

    assert good >= 0.95, f"good worker mean={good}"
    assert bad <= 0.1, f"bad worker mean={bad}"
