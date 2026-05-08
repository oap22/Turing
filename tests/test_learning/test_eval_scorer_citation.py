"""citation_exact_match: voice-independent, mechanical."""

from __future__ import annotations

from turing.learning.eval_set import citation_exact_match


def test_perfect_match_returns_one() -> None:
    score = citation_exact_match(
        output="As shown in [1] and [2], LLMs hallucinate.",
        expected_citations=["[1]", "[2]"],
    )
    assert score == 1.0


def test_one_of_two_citations_present_returns_half() -> None:
    score = citation_exact_match(
        output="As shown in [1], the model fails.",
        expected_citations=["[1]", "[2]"],
    )
    assert score == 0.5


def test_no_citations_present_returns_zero() -> None:
    score = citation_exact_match(
        output="LLMs hallucinate.",
        expected_citations=["[1]", "[2]"],
    )
    assert score == 0.0


def test_empty_expected_returns_one() -> None:
    """Nothing to match means nothing was missed."""
    score = citation_exact_match(output="anything", expected_citations=[])
    assert score == 1.0


def test_substring_does_not_count_as_match() -> None:
    """Citation strings must appear verbatim — corrupted citations
    (e.g. ``[12]`` when ``[1]`` was expected) must not silently pass."""
    score = citation_exact_match(
        output="See reference [12] for details.",
        expected_citations=["[1]"],
    )
    assert score == 0.0


def test_mutation_swap_one_char_drops_score() -> None:
    """Mutation: swap a digit. The corrupted output must score below 1.0
    so the gate catches a downstream paraphraser that mangles citations."""
    good = "As shown in [1] and [2], LLMs hallucinate."
    bad = good.replace("[2]", "[3]")
    expected = ["[1]", "[2]"]
    assert citation_exact_match(good, expected) == 1.0
    assert citation_exact_match(bad, expected) < 1.0
