"""End-to-end test for the flywheel training cycle (issue #268, ADR 0009 slice 3).

Curated answers → accumulated dataset → from-base LoRA → collapse-aware eval
gate → promote → ×4 rollout, turning the whole flywheel once with the trainer
and held-out eval mocked (no GPU). Also exercises slice 3b (#271, collapse gate
blocks promotion) and the 1-3-round cap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from turing.coordinator.adapters import AdapterRegistry, AdapterState
from turing.coordinator.flywheel.generalist import (
    AI_ML_GENERALIST,
    GeneralistAdapterRollout,
    GeneralistFleet,
    replicate_to_fleet,
)
from turing.coordinator.flywheel.morning_curation import SFTCandidate
from turing.learning.eval_set.collapse_gate import CollapseAwareEvalGate, EvalRun
from turing.learning.trainer import (
    CycleRejectedError,
    FlywheelTrainingCycle,
    InMemoryObjectStore,
    LoraRecipe,
    RoundCapGuard,
    StubTrainer,
    TrainerPublisher,
)
from turing.learning.trainer.lora_recipe import RoundCapExceededError
from turing.learning.trainer.sft_dataset_builder import SFTDatasetBuilder
from turing.transport.signer import MessageSigner

if TYPE_CHECKING:
    from pathlib import Path

BASE_MODEL = "qwen2.5-7b"


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def notify(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


def _cand(n: int) -> SFTCandidate:
    return SFTCandidate(
        question=f"q{n}",
        reasoning=f"reasoning {n}",
        answer=f"answer {n}",
        specialty=AI_ML_GENERALIST,
    )


def _diverse(score: float) -> EvalRun:
    return EvalRun(
        scores=(score, score, score),
        outputs=("alpha beta gamma", "delta epsilon zeta", "eta theta iota"),
    )


def _build_cycle(
    tmp_path: Path,
    *,
    held_out,
    eval_gate: CollapseAwareEvalGate | None = None,
    guard: RoundCapGuard | None = None,
    seeds=(),
) -> tuple[FlywheelTrainingCycle, AdapterRegistry]:
    signer = MessageSigner.generate()
    registry = AdapterRegistry(worker_base_model=BASE_MODEL, trusted_issuers=[signer.public_key])
    fleet = GeneralistFleet(workers=("jetson-1", "jetson-2", "jetson-3", "jetson-4"))
    rollout = GeneralistAdapterRollout(fleet=fleet, registry=registry, notifier=_Recorder())
    cycle = FlywheelTrainingCycle(
        dataset_builder=SFTDatasetBuilder(specialty=AI_ML_GENERALIST, seeds=seeds),
        recipe=LoraRecipe(),
        round_guard=guard or RoundCapGuard(),
        trainer=StubTrainer(eval_score=0.8),
        publisher=TrainerPublisher(
            signer=signer, object_store=InMemoryObjectStore(), facility_name="dgx-1"
        ),
        rollout=rollout,
        fleet=fleet,
        eval_gate=eval_gate or CollapseAwareEvalGate(min_delta=0.02),
        held_out_eval=held_out,
        artifact_dir=tmp_path / "artifacts",
    )
    return cycle, registry


def test_flywheel_turns_once_end_to_end(tmp_path: Path) -> None:
    """Curated pairs → dataset → from-base LoRA → gate → promote → ×4 rollout."""
    # Candidate beats baseline and keeps diversity → passes the gate.
    held_out = lambda v: (_diverse(0.60), _diverse(0.80))  # noqa: E731
    cycle, registry = _build_cycle(tmp_path, held_out=held_out)

    report = cycle.run_cycle(base_model=BASE_MODEL, curated=[_cand(1), _cand(2)], version="v1")

    assert report.promoted
    assert report.round_number == 1
    assert report.dataset_examples == 2
    assert report.mean_delta == pytest.approx(0.20)
    # Adapter is LIVE in the registry...
    assert registry.state_of(name=report.adapter_name, version="v1") is AdapterState.LIVE
    # ...and the rollout replicates to all four workers.
    fleet = GeneralistFleet(workers=("jetson-1", "jetson-2", "jetson-3", "jetson-4"))
    replicate_to_fleet(report.rollout, fleet=fleet, version="v1", score=0.80)
    from turing.coordinator.promotion.rollout import RolloutState

    assert report.rollout.state is RolloutState.COMPLETED


def test_dataset_accumulates_seed_plus_curated(tmp_path: Path) -> None:
    held_out = lambda v: (_diverse(0.60), _diverse(0.80))  # noqa: E731
    cycle, _ = _build_cycle(tmp_path, held_out=held_out, seeds=[_cand(99)])

    report = cycle.run_cycle(base_model=BASE_MODEL, curated=[_cand(1)], version="v1")

    # Frozen seed + the new curated pair both made it into the run.
    assert report.dataset_examples == 2


def test_collapse_blocks_promotion_even_when_mean_rises(tmp_path: Path) -> None:
    """Slice 3b (#271): mean accuracy up, but diversity collapses → no promote."""
    baseline = _diverse(0.50)
    collapsed = EvalRun(scores=(0.90, 0.90, 0.90), outputs=("same", "same", "same"))
    cycle, registry = _build_cycle(
        tmp_path,
        held_out=lambda v: (baseline, collapsed),
        eval_gate=CollapseAwareEvalGate(min_delta=0.02, max_diversity_drop=0.10),
    )

    with pytest.raises(CycleRejectedError):
        cycle.run_cycle(base_model=BASE_MODEL, curated=[_cand(1)], version="v1")

    # The adapter was registered (audit trail) but never promoted to LIVE.
    assert registry.state_of(name="dgx-1/qwen2.5-7b", version="v1") is AdapterState.STAGED


def test_flat_mean_blocks_promotion(tmp_path: Path) -> None:
    cycle, _ = _build_cycle(
        tmp_path,
        held_out=lambda v: (_diverse(0.70), _diverse(0.705)),  # +0.005 < 0.02
    )
    with pytest.raises(CycleRejectedError):
        cycle.run_cycle(base_model=BASE_MODEL, curated=[_cand(1)], version="v1")


def test_round_cap_stops_a_fourth_cycle(tmp_path: Path) -> None:
    guard = RoundCapGuard()
    held_out = lambda v: (_diverse(0.50), _diverse(0.80))  # noqa: E731
    cycle, _ = _build_cycle(tmp_path, held_out=held_out, guard=guard)

    for i in range(3):
        cycle.run_cycle(base_model=BASE_MODEL, curated=[_cand(i)], version=f"v{i}")
    with pytest.raises(RoundCapExceededError):
        cycle.run_cycle(base_model=BASE_MODEL, curated=[_cand(99)], version="v99")
