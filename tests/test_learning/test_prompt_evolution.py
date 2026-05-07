"""Phase A — nightly prompt + exemplar evolution with A/B promotion (#21).

Three deliverables:

1. ``Episode.prompt_version`` recorded so A/B comparisons are sound.
2. ``PromotionGate`` documents and enforces win-rate / sample-size / holdout
   policy with synthetic episode fixtures.
3. ``PromptEvolver`` runs nightly: pulls top-K positive episodes per
   specialty, mines exemplars (reusing ``learning/patterns.py``), regenerates
   the worker's system prompt + few-shot exemplars, registers the new
   ``prompt_version`` as a candidate, and lets the gate decide promotion.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.learning.prompt_evolution import (
    ABRouter,
    PromotionDecision,
    PromotionGate,
    PromptEvolver,
    PromptVersionRegistry,
)


def _episode(
    *,
    subtask_id: str,
    specialty: str = "research-summarize",
    prompt_version: str = "v1",
    critic_score: float = 0.5,
    outcome: SubtaskState = SubtaskState.COMPLETED,
) -> Episode:
    return Episode(
        task_id=f"t-{subtask_id}",
        subtask_id=subtask_id,
        worker_id="w",
        specialty=specialty,
        model_version="qwen2.5-7b",
        adapter_version="base",
        input_text="i",
        trajectory=("a",),
        output_text="o",
        success=outcome is SubtaskState.COMPLETED,
        latency_ms=1,
        tokens_used=1,
        outcome=outcome,
        critic_score=critic_score,
        recorded_at_ms=0,
        prompt_version=prompt_version,
    )


# ── Episode.prompt_version ────────────────────────────────────────────


class TestEpisodePromptVersion:
    def test_prompt_version_recorded_on_episode(self) -> None:
        ep = _episode(subtask_id="s-1", prompt_version="research-summarize@v3")
        assert ep.prompt_version == "research-summarize@v3"

    def test_prompt_version_default_blank_for_legacy(self) -> None:
        # Constructed without the field — kept for backwards-compat with
        # episodes recorded before this slice.
        ep = Episode(
            task_id="t",
            subtask_id="s",
            worker_id="w",
            specialty="x",
            model_version="m",
            adapter_version="a",
            input_text="i",
            trajectory=("a",),
            output_text="o",
            success=True,
            latency_ms=1,
            tokens_used=1,
            outcome=SubtaskState.COMPLETED,
            critic_score=0.5,
            recorded_at_ms=0,
        )
        assert ep.prompt_version == ""


# ── PromotionGate ─────────────────────────────────────────────────────


class TestPromotionGate:
    def test_promotes_when_win_rate_beats_threshold(self) -> None:
        gate = PromotionGate(
            min_sample_size=10, win_rate_threshold=0.55, holdout_min_size=5
        )
        # 8 wins out of 10 → 80% > 55% with required holdout
        candidate = [_episode(subtask_id=f"c{i}", critic_score=0.9) for i in range(8)]
        candidate += [_episode(subtask_id=f"c{i}", critic_score=0.1) for i in range(8, 10)]
        baseline = [_episode(subtask_id=f"b{i}", critic_score=0.4) for i in range(10)]
        decision = gate.evaluate(candidate=candidate, baseline=baseline)
        assert decision is PromotionDecision.PROMOTE

    def test_holds_when_sample_size_too_small(self) -> None:
        gate = PromotionGate(
            min_sample_size=10, win_rate_threshold=0.55, holdout_min_size=5
        )
        candidate = [_episode(subtask_id=f"c{i}", critic_score=0.9) for i in range(3)]
        baseline = [_episode(subtask_id=f"b{i}", critic_score=0.4) for i in range(10)]
        decision = gate.evaluate(candidate=candidate, baseline=baseline)
        assert decision is PromotionDecision.HOLD_INSUFFICIENT_DATA

    def test_holds_when_baseline_holdout_too_small(self) -> None:
        gate = PromotionGate(
            min_sample_size=10, win_rate_threshold=0.55, holdout_min_size=5
        )
        candidate = [_episode(subtask_id=f"c{i}", critic_score=0.9) for i in range(10)]
        baseline = [_episode(subtask_id=f"b{i}", critic_score=0.4) for i in range(2)]
        decision = gate.evaluate(candidate=candidate, baseline=baseline)
        assert decision is PromotionDecision.HOLD_INSUFFICIENT_DATA

    def test_rejects_when_win_rate_below_threshold(self) -> None:
        gate = PromotionGate(
            min_sample_size=10, win_rate_threshold=0.55, holdout_min_size=5
        )
        candidate = [_episode(subtask_id=f"c{i}", critic_score=0.4) for i in range(10)]
        baseline = [_episode(subtask_id=f"b{i}", critic_score=0.6) for i in range(10)]
        decision = gate.evaluate(candidate=candidate, baseline=baseline)
        assert decision is PromotionDecision.REJECT


# ── ABRouter ──────────────────────────────────────────────────────────


class TestABRouter:
    def test_zero_percent_routes_all_to_baseline(self) -> None:
        registry = PromptVersionRegistry()
        registry.set_baseline("research-summarize", "v1")
        registry.set_candidate("research-summarize", "v2")
        router = ABRouter(registry=registry, candidate_share=0.0)
        choices = {
            router.choose("research-summarize", task_id=f"t-{i}") for i in range(50)
        }
        assert choices == {"v1"}

    def test_full_share_routes_all_to_candidate(self) -> None:
        registry = PromptVersionRegistry()
        registry.set_baseline("research-summarize", "v1")
        registry.set_candidate("research-summarize", "v2")
        router = ABRouter(registry=registry, candidate_share=1.0)
        choices = {
            router.choose("research-summarize", task_id=f"t-{i}") for i in range(50)
        }
        assert choices == {"v2"}

    def test_share_split_is_deterministic_per_task_id(self) -> None:
        registry = PromptVersionRegistry()
        registry.set_baseline("research-summarize", "v1")
        registry.set_candidate("research-summarize", "v2")
        router = ABRouter(registry=registry, candidate_share=0.5)
        first = router.choose("research-summarize", task_id="task-42")
        second = router.choose("research-summarize", task_id="task-42")
        assert first == second

    def test_no_candidate_returns_baseline(self) -> None:
        registry = PromptVersionRegistry()
        registry.set_baseline("research-summarize", "v1")
        router = ABRouter(registry=registry, candidate_share=0.5)
        assert router.choose("research-summarize", task_id="t-1") == "v1"


# ── PromptEvolver wiring ──────────────────────────────────────────────


class TestPromptEvolver:
    @pytest.mark.asyncio
    async def test_evolver_reuses_pattern_extractor(self) -> None:
        """The evolver mines exemplars by delegating to PatternExtractor."""
        store = EpisodeStore()
        for i in range(5):
            store.record(
                _episode(subtask_id=f"s{i}", critic_score=0.9 + 0.001 * i)
            )

        registry = PromptVersionRegistry()
        registry.set_baseline("research-summarize", "v1")

        pattern_extractor = MagicMock()
        pattern_extractor.extract = AsyncMock(
            return_value={"patterns": ["use citations"], "facts": [], "preferences": []}
        )

        evolver = PromptEvolver(
            episode_store=store,
            registry=registry,
            pattern_extractor=pattern_extractor,
            top_k=3,
        )
        version = await evolver.evolve("research-summarize")

        # The evolver registered the candidate
        assert registry.candidate_for("research-summarize") == version
        # And it actually called the existing pattern extractor (reuse, not rewrite)
        pattern_extractor.extract.assert_awaited()

    @pytest.mark.asyncio
    async def test_evolver_no_candidate_when_no_episodes(self) -> None:
        store = EpisodeStore()
        registry = PromptVersionRegistry()
        registry.set_baseline("research-summarize", "v1")
        pattern_extractor = MagicMock()
        pattern_extractor.extract = AsyncMock(return_value={})

        evolver = PromptEvolver(
            episode_store=store,
            registry=registry,
            pattern_extractor=pattern_extractor,
        )
        version = await evolver.evolve("research-summarize")
        assert version is None
        assert registry.candidate_for("research-summarize") is None
