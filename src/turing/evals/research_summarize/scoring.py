"""Scoring functions for `research-summarize` eval cases.

All scoring functions take a `summary: str` and an `EvalCase`, return a
`ScoreResult` with an overall float in [0, 1] plus a breakdown dict for
debugging. Aggregation across cases happens in `harness.py`.

All three scorers are deterministic v1 implementations. Claim preservation
uses substring + token-overlap; voice match uses style metrics derived from
the operator's vault sample; citation correctness checks wiki-link presence
against required source IDs. v2 swaps in embedding similarity (claims) and
LLM-judge proximity (citations).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .schema import EvalCase


class Embedder(Protocol):
    """Synchronous batched embedder. Project's `EmbeddingModel` is async,
    so the harness wraps it; the scorer itself stays sync for simplicity."""

    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


DEFAULT_CLAIM_THRESHOLD = 0.75


@dataclass
class ScoreResult:
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _summary_chunks(summary: str) -> list[str]:
    """Sentence-level chunks for embedding comparison."""
    return [s.strip() for s in _SENT_RE.split(summary.strip()) if s.strip()]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _claim_present(
    claim: str,
    summary_norm: str,
    *,
    embedder: Embedder | None = None,
    summary_chunks: list[str] | None = None,
    threshold: float = DEFAULT_CLAIM_THRESHOLD,
) -> bool:
    """Check whether a claim is present in the summary.

    Order of attempts:
      1. Exact substring (fast path; skips the embedder entirely)
      2. Embedding cosine ≥ threshold against any summary sentence (if embedder)
      3. Token-overlap fallback (≥70% of content tokens present)
    """
    claim_norm = _normalize(claim)
    if claim_norm in summary_norm:
        return True

    if embedder is not None and summary_chunks:
        vecs = embedder.embed_batch([claim, *summary_chunks])
        claim_vec = vecs[0]
        return any(_cosine(claim_vec, chunk_vec) >= threshold for chunk_vec in vecs[1:])

    tokens = [t for t in re.findall(r"\w+", claim_norm) if len(t) > 2]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in summary_norm)
    return hits / len(tokens) >= 0.7


def score_claim_preservation_v1(
    summary: str,
    case: EvalCase,
    *,
    embedder: Embedder | None = None,
) -> ScoreResult:
    """Reward must-contain claims; penalize must-not-contain leaks.

    If `embedder` is provided, claims that miss the substring fast path are
    re-checked against summary sentences via cosine similarity. The threshold
    is `case.expected.claim_match_threshold` (default 0.75).
    """
    summary_norm = _normalize(summary)
    chunks = _summary_chunks(summary)
    threshold = case.expected.claim_match_threshold or DEFAULT_CLAIM_THRESHOLD
    must = case.expected.must_contain_claims
    must_not = case.expected.must_not_contain

    def present(c: str) -> bool:
        return _claim_present(
            c,
            summary_norm,
            embedder=embedder,
            summary_chunks=chunks,
            threshold=threshold,
        )

    if not must:
        recall = 1.0
        hits = 0
    else:
        hits = sum(1 for c in must if present(c))
        recall = hits / len(must)

    # Strict substring for forbidden phrases: hallucination guards target
    # literal phrasings, not paraphrases that happen to share vocabulary.
    leaks = sum(1 for c in must_not if _normalize(c) in summary_norm)
    leak_penalty = min(1.0, leaks * 0.34)  # 3 leaks = full penalty

    score = max(0.0, recall - leak_penalty)
    return ScoreResult(
        score=score,
        breakdown={
            "recall": recall,
            "hits": float(hits),
            "leaks": float(leaks),
            "leak_penalty": leak_penalty,
        },
        notes=[f"missing: {c!r}" for c in must if not present(c)]
        + [f"leaked: {c!r}" for c in must_not if _normalize(c) in summary_norm],
    )


_HEDGE_TOKENS = {
    "perhaps",
    "maybe",
    "possibly",
    "might",
    "may",
    "could",
    "somewhat",
    "arguably",
    "presumably",
    "likely",
    "seemingly",
    "apparently",
}
_FIRST_PERSON = {"i", "me", "my", "we", "us", "our"}
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_WIKILINK = re.compile(r"\[\[[^\]]+\]\]")
_HAS_DIGIT = re.compile(r"\d")


def _split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]


def _word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))


def score_voice_match_v1(summary: str, case: EvalCase) -> ScoreResult:
    """Deterministic style metrics against the operator's voice features.

    Each configured feature contributes one pass/fail axis; final score is
    the fraction passing. Failures are listed in `notes` so authoring drift
    is visible without re-reading the case.
    """
    vf = case.expected.voice_features
    if vf is None:
        return ScoreResult(score=1.0, notes=["no voice_features configured"])

    summary_norm = _normalize(summary)
    sentences = _split_sentences(summary)
    paragraphs = [p for p in _PARAGRAPH_SPLIT.split(summary.strip()) if p.strip()]
    words = re.findall(r"\w+", summary_norm)
    total_words = max(1, len(words))

    axes: dict[str, float] = {}
    failures: list[str] = []

    if vf.max_avg_sentence_len is not None and sentences:
        avg = sum(_word_count(s) for s in sentences) / len(sentences)
        ok = avg <= vf.max_avg_sentence_len
        axes["avg_sentence_len"] = 1.0 if ok else 0.0
        if not ok:
            failures.append(f"avg sentence len {avg:.1f} > {vf.max_avg_sentence_len}")

    if vf.forbid_phrases:
        bad = [p for p in vf.forbid_phrases if p.lower() in summary_norm]
        axes["forbid_phrases"] = 1.0 if not bad else 0.0
        if bad:
            failures.append(f"used forbidden phrases: {bad}")

    if vf.require_first_person is not None:
        has_fp = any(t in _FIRST_PERSON for t in words)
        ok = has_fp if vf.require_first_person else not has_fp
        axes["first_person"] = 1.0 if ok else 0.0
        if not ok:
            failures.append(
                "missing first person" if vf.require_first_person else "used first person"
            )

    if vf.hedge_density_max is not None:
        hedge_count = sum(1 for t in words if t in _HEDGE_TOKENS)
        density = hedge_count / total_words
        ok = density <= vf.hedge_density_max
        axes["hedge_density"] = 1.0 if ok else 0.0
        if not ok:
            failures.append(f"hedge density {density:.3f} > {vf.hedge_density_max}")

    if vf.max_paragraphs is not None:
        ok = len(paragraphs) <= vf.max_paragraphs
        axes["max_paragraphs"] = 1.0 if ok else 0.0
        if not ok:
            failures.append(f"paragraphs {len(paragraphs)} > {vf.max_paragraphs}")

    if vf.max_sentences_per_paragraph is not None:
        worst = max((len(_split_sentences(p)) for p in paragraphs), default=0)
        ok = worst <= vf.max_sentences_per_paragraph
        axes["max_sent_per_para"] = 1.0 if ok else 0.0
        if not ok:
            failures.append(
                f"longest paragraph has {worst} sentences > {vf.max_sentences_per_paragraph}"
            )

    if vf.min_em_dash_count is not None:
        # ASCII " -- " is the operator's convention; also accept unicode em-dash.
        count = summary.count(" -- ") + summary.count("—")
        ok = count >= vf.min_em_dash_count
        axes["em_dashes"] = 1.0 if ok else 0.0
        if not ok:
            failures.append(f"em-dash count {count} < {vf.min_em_dash_count}")

    if vf.min_wikilink_count is not None:
        count = len(_WIKILINK.findall(summary))
        ok = count >= vf.min_wikilink_count
        axes["wikilinks"] = 1.0 if ok else 0.0
        if not ok:
            failures.append(f"wikilink count {count} < {vf.min_wikilink_count}")

    if vf.require_concrete_numbers:
        ok = bool(_HAS_DIGIT.search(summary))
        axes["concrete_numbers"] = 1.0 if ok else 0.0
        if not ok:
            failures.append("no digit-bearing tokens")

    if not axes:
        return ScoreResult(score=1.0, notes=["no voice axes evaluated"])
    score = sum(axes.values()) / len(axes)
    return ScoreResult(score=score, breakdown=axes, notes=failures)


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0.0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _wikilinks_in(text: str) -> list[str]:
    return [m.strip("[]") for m in _WIKILINK.findall(text)]


def _presence_score(summary: str, case: EvalCase) -> ScoreResult:
    """v1 fallback: just check each required src_id appears as [[src_id]]."""
    valid_ids = {d.id for d in case.source_docs}
    cited = set(_wikilinks_in(summary))
    required = set(case.expected.required_citations.keys())

    if not required:
        return ScoreResult(score=1.0, notes=["no required citations"])

    precision = len(cited & valid_ids) / len(cited) if cited else 0.0
    recall = len(required & cited) / len(required)
    f1 = _f1(precision, recall)

    notes = []
    bad = cited - valid_ids
    missing = required - cited
    if bad:
        notes.append(f"cited unknown sources: {sorted(bad)}")
    if missing:
        notes.append(f"missing required citations: {sorted(missing)}")

    return ScoreResult(
        score=f1,
        breakdown={"precision": precision, "recall": recall, "f1": f1},
        notes=notes,
    )


def score_citation_correctness_v1(
    summary: str,
    case: EvalCase,
    *,
    judge: Any = None,
) -> ScoreResult:
    """Grade wiki-link citations.

    Behaviour:
      - No `required_citations` -> trivially 1.0.
      - Without `judge`, falls back to presence-only F1 (existing behaviour).
      - With `judge`, applies sentence-level proximity + judge verdicts:
        * Recall  = (required (claim, src_id) pairs grounded) / total required
        * Precision = (citations made that judge says 'supports' the
          claim in their sentence) / total citations made
        * Score = F1
    """
    has_claim_text = any(case.expected.required_citations.values())
    if judge is None or not has_claim_text:
        return _presence_score(summary, case)

    valid_ids = {d.id for d in case.source_docs}
    src_text = {d.id: d.text for d in case.source_docs}
    sentences = [s.strip() for s in _SENT_RE.split(summary.strip()) if s.strip()]

    # Required (src_id, claim_text) pairs the operator authored as ground truth.
    required_pairs = [
        (sid, claim) for sid, claims in case.expected.required_citations.items() for claim in claims
    ]

    # Recall: each required pair counts iff some sentence contains the claim
    # AND cites src_id AND the judge confirms support.
    grounded = 0
    missing_notes: list[str] = []
    for sid, claim in required_pairs:
        for sent in sentences:
            if sid not in _wikilinks_in(sent):
                continue
            if not _claim_in_sentence(claim, sent):
                continue
            verdict = judge.judge(claim=claim, source_id=sid, source_text=src_text.get(sid, ""))
            if verdict == "supports":
                grounded += 1
                break
        else:
            missing_notes.append(f"ungrounded: src={sid!r} claim={claim!r}")
    recall = grounded / len(required_pairs) if required_pairs else 1.0

    # Precision: every (sentence, cited_src_id) the worker actually wrote must
    # match a required pair AND have judge='supports' for the sentence's text.
    correct_citations = 0
    total_citations = 0
    bad_notes: list[str] = []
    for sent in sentences:
        for sid in _wikilinks_in(sent):
            if sid not in valid_ids:
                bad_notes.append(f"cited unknown source: {sid!r}")
                total_citations += 1
                continue
            total_citations += 1
            verdict = judge.judge(claim=sent, source_id=sid, source_text=src_text.get(sid, ""))
            if verdict == "supports":
                correct_citations += 1
            else:
                bad_notes.append(f"unsupported citation: src={sid!r} verdict={verdict!r}")
    precision = correct_citations / total_citations if total_citations else 0.0

    f1 = _f1(precision, recall)
    return ScoreResult(
        score=f1,
        breakdown={"precision": precision, "recall": recall, "f1": f1},
        notes=missing_notes + bad_notes,
    )


def _claim_in_sentence(claim: str, sentence: str) -> bool:
    """Heuristic: claim is 'in' the sentence if normalised substring or ≥60%
    of content tokens overlap. Cheap and good enough for grading; the judge
    is the authoritative call."""
    claim_n = _normalize(claim)
    sent_n = _normalize(sentence)
    if claim_n in sent_n:
        return True
    tokens = [t for t in re.findall(r"\w+", claim_n) if len(t) > 2]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in sent_n)
    return hits / len(tokens) >= 0.6


SCORERS: dict[str, Any] = {
    "score_claim_preservation_v1": score_claim_preservation_v1,
    "score_voice_match_v1": score_voice_match_v1,
    "score_citation_correctness_v1": score_citation_correctness_v1,
}
