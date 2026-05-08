"""voice_cosine: cosine similarity to a stored voice-reference embedding."""

from __future__ import annotations

import math

from turing.learning.eval_set import voice_cosine


def _embed(text: str) -> list[float]:
    """Tiny deterministic embedding — bag-of-letters frequency vector.

    We don't need a real model for these tests; we need a stable function
    that returns similar vectors for similar text and dissimilar vectors
    for dissimilar text. Bag-of-letters is enough for the contrast.
    """
    return [text.lower().count(chr(ord("a") + i)) for i in range(26)]


def test_identical_text_scores_one() -> None:
    voice_ref = _embed("the cluster's voice")
    score = voice_cosine(output="the cluster's voice", voice_ref_embedding=voice_ref, embed=_embed)
    assert math.isclose(score, 1.0, abs_tol=1e-9)


def test_paraphrase_scores_high_but_not_one() -> None:
    voice_ref = _embed("LLMs hallucinate at long contexts.")
    score = voice_cosine(
        output="Long contexts cause hallucination in language models.",
        voice_ref_embedding=voice_ref,
        embed=_embed,
    )
    # Paraphrase shares vocabulary but is rewritten — score is high but
    # not a perfect 1.0.
    assert 0.85 < score < 1.0


def test_unrelated_text_scores_below_paraphrase() -> None:
    voice_ref = _embed("LLMs hallucinate at long contexts.")
    para = voice_cosine(
        "Long contexts cause hallucination in language models.",
        voice_ref,
        embed=_embed,
    )
    far = voice_cosine(
        "purple zebras dance under the moon",
        voice_ref,
        embed=_embed,
    )
    assert far < para


def test_score_clamped_to_zero_one_range() -> None:
    voice_ref = _embed("anything")
    score = voice_cosine("anything", voice_ref, embed=_embed)
    assert 0.0 <= score <= 1.0


def test_zero_vector_returns_zero_safely() -> None:
    """If the embedder produces a zero vector for empty input we must not
    divide by zero — return 0.0 and let the caller decide."""
    voice_ref = [0.0] * 26
    score = voice_cosine("anything", voice_ref, embed=_embed)
    assert score == 0.0


def test_mutation_paraphrase_drops_score_below_threshold() -> None:
    """Mutation: replace the output with neutralised text. The score
    must drop below a paraphrase-grade threshold (0.85) so the gate
    catches a worker that adopts a foreign voice."""
    voice_ref = _embed("Brief, citation-heavy, slightly skeptical of model claims.")
    on_voice = voice_cosine(
        "Brief, citation-heavy, slightly skeptical of model claims.",
        voice_ref,
        embed=_embed,
    )
    off_voice = voice_cosine(
        "wow such cool, very neural, much intelligence!!!",
        voice_ref,
        embed=_embed,
    )
    assert on_voice > 0.85
    assert off_voice < 0.85
