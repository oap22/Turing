"""Reward attribution for thumbs feedback (slice 15/26, #17).

Subtask-thread thumbs → that episode (±1, full weight).
Main-message thumbs → synthesis episode (±1) + fractional credit (±0.3) to
non-synthesis subtasks whose workspace keys synthesis read.
No feedback in 24h → critic_score becomes the reward.
"""

from __future__ import annotations

from turing.coordinator.reward_attribution import (
    FEEDBACK_TIMEOUT_MS,
    SUBTASK_FRACTIONAL_FROM_SYNTHESIS,
    RewardAssignment,
    attribute_main_thumb,
    attribute_subtask_thumb,
    critic_fallback_rewards,
)


class TestSubtaskThumb:
    def test_positive_routes_full_weight_to_subtask(self) -> None:
        assignments = attribute_subtask_thumb(subtask_id="st_a", positive=True)
        assert assignments == [RewardAssignment(subtask_id="st_a", score=1.0)]

    def test_negative_routes_full_weight_to_subtask(self) -> None:
        assignments = attribute_subtask_thumb(subtask_id="st_a", positive=False)
        assert assignments == [RewardAssignment(subtask_id="st_a", score=-1.0)]

    def test_does_not_leak_to_other_subtasks(self) -> None:
        assignments = attribute_subtask_thumb(subtask_id="st_a", positive=True)
        assert {a.subtask_id for a in assignments} == {"st_a"}


class TestMainThumb:
    def test_positive_gives_synthesis_full_and_inputs_fractional(self) -> None:
        assignments = attribute_main_thumb(
            synthesis_subtask_id="st_synthesis",
            synthesis_input_subtasks=("st_a", "st_b"),
            positive=True,
        )
        scores = {a.subtask_id: a.score for a in assignments}
        assert scores == {
            "st_synthesis": 1.0,
            "st_a": SUBTASK_FRACTIONAL_FROM_SYNTHESIS,
            "st_b": SUBTASK_FRACTIONAL_FROM_SYNTHESIS,
        }

    def test_negative_inverts_signs(self) -> None:
        assignments = attribute_main_thumb(
            synthesis_subtask_id="st_synthesis",
            synthesis_input_subtasks=("st_a",),
            positive=False,
        )
        scores = {a.subtask_id: a.score for a in assignments}
        assert scores == {
            "st_synthesis": -1.0,
            "st_a": -SUBTASK_FRACTIONAL_FROM_SYNTHESIS,
        }

    def test_no_inputs_only_synthesis_credit(self) -> None:
        assignments = attribute_main_thumb(
            synthesis_subtask_id="st_synthesis",
            synthesis_input_subtasks=(),
            positive=True,
        )
        assert assignments == [RewardAssignment(subtask_id="st_synthesis", score=1.0)]


class TestCriticFallback:
    def test_after_24h_critic_score_becomes_reward(self) -> None:
        recorded = {"st_a": 0, "st_b": 0}
        critic = {"st_a": 0.7, "st_b": -0.4}
        now = FEEDBACK_TIMEOUT_MS  # exactly 24h elapsed
        assignments = critic_fallback_rewards(
            recorded_at_ms=recorded,
            critic_scores=critic,
            now_ms=now,
        )
        scores = {a.subtask_id: a.score for a in assignments}
        assert scores == {"st_a": 0.7, "st_b": -0.4}

    def test_before_24h_no_fallback(self) -> None:
        recorded = {"st_a": 0}
        critic = {"st_a": 0.5}
        now = FEEDBACK_TIMEOUT_MS - 1
        assert (
            critic_fallback_rewards(
                recorded_at_ms=recorded, critic_scores=critic, now_ms=now
            )
            == []
        )

    def test_skips_subtasks_without_critic_score(self) -> None:
        recorded = {"st_a": 0, "st_b": 0}
        critic = {"st_a": 0.3}  # st_b has no critic score
        now = FEEDBACK_TIMEOUT_MS + 1000
        assignments = critic_fallback_rewards(
            recorded_at_ms=recorded, critic_scores=critic, now_ms=now
        )
        assert {a.subtask_id for a in assignments} == {"st_a"}

    def test_custom_timeout(self) -> None:
        recorded = {"st_a": 0}
        critic = {"st_a": 0.9}
        # 1 minute timeout
        assignments = critic_fallback_rewards(
            recorded_at_ms=recorded,
            critic_scores=critic,
            now_ms=60_000,
            timeout_ms=60_000,
        )
        assert assignments == [RewardAssignment(subtask_id="st_a", score=0.9)]
