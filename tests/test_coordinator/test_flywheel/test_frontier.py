"""Tests for the question frontier (issue #266, ADR 0009 §4).

Evol-Instruct expansion (in-depth + in-breadth) → explicit elimination (with
logged reasons) → ROUGE-L dedup gate. Only survivors of both gates are
eligible to enter the runnable queue.
"""

from __future__ import annotations

import pytest

from turing.coordinator.flywheel import (
    EvolutionAxis,
    QuestionFrontier,
    rouge_l,
)
from turing.coordinator.flywheel.frontier import EvolvedQuestion, default_eliminator


class StubEvolver:
    """Returns canned variants per axis, ignoring the question text."""

    def __init__(self, *, in_depth: list[str], in_breadth: list[str]) -> None:
        self._by_axis = {
            EvolutionAxis.IN_DEPTH: in_depth,
            EvolutionAxis.IN_BREADTH: in_breadth,
        }

    async def evolve(self, *, question: str, axis: EvolutionAxis) -> list[str]:
        return list(self._by_axis[axis])


# ── expansion shape ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expansion_produces_in_depth_and_in_breadth() -> None:
    frontier = QuestionFrontier(
        evolver=StubEvolver(
            in_depth=["How does QLoRA's NF4 quantisation affect gradient flow?"],
            in_breadth=["How does LoRA compare to full fine-tuning on memory?"],
        )
    )

    expanded = await frontier.expand("What is QLoRA?")

    axes = {q.axis for q in expanded}
    assert axes == {EvolutionAxis.IN_DEPTH, EvolutionAxis.IN_BREADTH}
    assert all(q.origin == "What is QLoRA?" for q in expanded)
    assert len(expanded) == 2


# ── elimination ──────────────────────────────────────────────────────────────


def test_eliminator_drops_low_information_and_copied() -> None:
    origin = "What is QLoRA fine-tuning and why does it save memory?"
    # Low-information: too few distinct content tokens.
    short = EvolvedQuestion(prompt="why qlora", axis=EvolutionAxis.IN_DEPTH, origin=origin)
    assert default_eliminator(short) is not None
    assert "low_information" in default_eliminator(short)

    # Copied: near-identical to the origin.
    copied = EvolvedQuestion(prompt=origin, axis=EvolutionAxis.IN_BREADTH, origin=origin)
    assert "copied" in (default_eliminator(copied) or "")

    # Empty: unanswerable.
    empty = EvolvedQuestion(prompt="   ", axis=EvolutionAxis.IN_DEPTH, origin=origin)
    assert "unanswerable" in (default_eliminator(empty) or "")

    # A substantive, distinct evolution survives.
    good = EvolvedQuestion(
        prompt="How does paged optimisation interact with NF4 quantisation in QLoRA?",
        axis=EvolutionAxis.IN_DEPTH,
        origin=origin,
    )
    assert default_eliminator(good) is None


@pytest.mark.asyncio
async def test_pipeline_drops_eliminated_with_reason() -> None:
    frontier = QuestionFrontier(
        evolver=StubEvolver(
            in_depth=["How does NF4 quantisation bound QLoRA adapter rank in practice?"],
            in_breadth=["why qlora"],  # low information → eliminated
        )
    )

    result = await frontier.process(seeds=["What is QLoRA?"])

    assert len(result.admitted) == 1
    assert result.admitted[0].axis is EvolutionAxis.IN_DEPTH
    assert len(result.eliminated) == 1
    dropped_q, reason = result.eliminated[0]
    assert dropped_q.prompt == "why qlora"
    assert "low_information" in reason


# ── dedup gate ───────────────────────────────────────────────────────────────


def test_rouge_l_basic_properties() -> None:
    assert rouge_l("the quick brown fox", "the quick brown fox") == pytest.approx(1.0)
    assert rouge_l("alpha beta gamma", "delta epsilon zeta") == 0.0


@pytest.mark.asyncio
async def test_dedup_rejects_near_duplicate_admits_novel() -> None:
    # Existing frontier already has a question about catastrophic forgetting.
    existing = ["How does LoRA mitigate catastrophic forgetting during fine-tuning?"]
    frontier = QuestionFrontier(
        evolver=StubEvolver(
            # Near-duplicate of the existing frontier entry (high ROUGE-L).
            in_depth=["How does LoRA mitigate catastrophic forgetting while fine-tuning?"],
            # A genuinely novel direction.
            in_breadth=["What batch size maximises Jetson Orin throughput for 7B inference?"],
        )
    )

    result = await frontier.process(seeds=["LoRA forgetting?"], existing_frontier=existing)

    admitted_prompts = [q.prompt for q in result.admitted]
    assert "What batch size maximises Jetson Orin throughput for 7B inference?" in admitted_prompts
    assert len(result.deduped) == 1
    dup_q, reason = result.deduped[0]
    assert "catastrophic forgetting while fine-tuning" in dup_q.prompt
    assert "near_duplicate" in reason


@pytest.mark.asyncio
async def test_dedup_rejects_internal_duplicates_among_survivors() -> None:
    """Two near-identical survivors of the same batch: the second is deduped."""
    dup = "How does NF4 quantisation reduce QLoRA memory during fine-tuning?"
    frontier = QuestionFrontier(
        evolver=StubEvolver(in_depth=[dup], in_breadth=[dup])
    )

    result = await frontier.process(seeds=["QLoRA memory?"])

    assert len(result.admitted) == 1  # only the first copy admitted
    assert len(result.deduped) == 1
