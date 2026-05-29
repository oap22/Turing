"""Tests for the LoRA recipe + round-cap guard (ADR 0009 §4).

- retrain-from-base (no adapter stacking),
- LoRA on all linear layers, modest rank, few epochs,
- 1-3 useful rounds then re-evaluate.
"""

from __future__ import annotations

import pytest

from turing.learning.trainer import (
    ALL_LINEAR_TARGET_MODULES,
    MAX_USEFUL_ROUNDS,
    InvalidRecipeError,
    LoraRecipe,
    RoundCapExceededError,
    RoundCapGuard,
)
from turing.learning.trainer.job import TrainingJob

# ── recipe defaults honour the guardrails ────────────────────────────────────


def test_default_recipe_targets_all_linear_layers() -> None:
    recipe = LoraRecipe()
    assert recipe.targets_all_linear_layers
    assert set(recipe.target_modules) == set(ALL_LINEAR_TARGET_MODULES)


def test_default_recipe_is_modest_rank_and_few_epochs() -> None:
    recipe = LoraRecipe()
    assert 1 <= recipe.rank <= 64
    assert recipe.epochs <= 4


def test_default_recipe_trains_from_base() -> None:
    assert LoraRecipe().train_from_base is True


# ── validation rejects out-of-envelope settings ──────────────────────────────


def test_too_many_epochs_rejected() -> None:
    with pytest.raises(InvalidRecipeError, match="few-epochs"):
        LoraRecipe(epochs=50)


def test_oversized_rank_rejected() -> None:
    with pytest.raises(InvalidRecipeError, match="rank"):
        LoraRecipe(rank=4096)


def test_train_from_base_cannot_be_disabled() -> None:
    with pytest.raises(InvalidRecipeError, match="train_from_base"):
        LoraRecipe(train_from_base=False)


def test_empty_target_modules_rejected() -> None:
    with pytest.raises(InvalidRecipeError, match="target_modules"):
        LoraRecipe(target_modules=())


# ── round-trips through the TrainingJob hyperparameters dict ──────────────────


def test_recipe_round_trips_through_hyperparameters() -> None:
    recipe = LoraRecipe(rank=8, epochs=3)
    hp = recipe.to_hyperparameters()
    assert hp["rank"] == 8
    assert hp["epochs"] == 3
    assert hp["target_modules"] == list(ALL_LINEAR_TARGET_MODULES)
    assert hp["train_from_base"] is True

    parsed = LoraRecipe.from_hyperparameters(hp)
    assert parsed == recipe


def test_from_hyperparameters_rejects_tampered_payload() -> None:
    # A payload that tries to sneak 50 epochs past the recipe is rejected.
    with pytest.raises(InvalidRecipeError):
        LoraRecipe.from_hyperparameters({"epochs": 50})


def test_recipe_feeds_a_valid_training_job() -> None:
    recipe = LoraRecipe()
    job = TrainingJob(
        job_id="sft-1",
        dataset_url="file:///tmp/ds.jsonl",
        dataset_sha256="0" * 64,
        base_model="qwen2.5-7b",
        method="sft",
        hyperparameters=recipe.to_hyperparameters(),
    )
    # The trainer can recover the recipe from the job it received.
    assert LoraRecipe.from_hyperparameters(job.hyperparameters).targets_all_linear_layers


# ── round-cap guard: 1-3 rounds then re-evaluate ──────────────────────────────


def test_round_cap_allows_up_to_three_rounds() -> None:
    guard = RoundCapGuard()
    assert MAX_USEFUL_ROUNDS == 3
    assert guard.check_and_increment("ai-ml-generalist") == 1
    assert guard.check_and_increment("ai-ml-generalist") == 2
    assert guard.check_and_increment("ai-ml-generalist") == 3
    assert guard.rounds_used("ai-ml-generalist") == 3


def test_round_cap_blocks_fourth_round() -> None:
    guard = RoundCapGuard()
    for _ in range(MAX_USEFUL_ROUNDS):
        guard.check_and_increment("s")
    with pytest.raises(RoundCapExceededError):
        guard.check_and_increment("s")


def test_round_cap_reset_reopens_window() -> None:
    guard = RoundCapGuard()
    for _ in range(MAX_USEFUL_ROUNDS):
        guard.check_and_increment("s")
    guard.reset("s")  # human re-evaluated
    assert guard.rounds_used("s") == 0
    assert guard.check_and_increment("s") == 1


def test_round_cap_is_per_specialty() -> None:
    guard = RoundCapGuard()
    guard.check_and_increment("a")
    guard.check_and_increment("a")
    # A different specialty has its own independent budget.
    assert guard.check_and_increment("b") == 1
    assert guard.rounds_used("a") == 2
