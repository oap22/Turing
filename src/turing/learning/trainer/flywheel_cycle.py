"""FlywheelTrainingCycle — one end-to-end turn of the training flywheel.

ADR 0009 vertical slice 3 (#268): curated answers become a **trained,
eval-gated, fleet-deployed adapter**, with the §4 guardrails composed in one
demonstrable cut:

    curated SFT candidates
      → SFTDatasetBuilder      (accumulate-never-replace + reasoning-bearing)
      → LoraRecipe             (from-base, all-linear, few-epochs)
      → RoundCapGuard          (1-3 useful rounds, then re-evaluate)
      → trainer.train          (CUDA LoRA on H100/DGX; injected)
      → TrainerPublisher       (SHA256 + Ed25519 signed manifest)
      → AdapterRegistry.register (verify on load → STAGED)
      → CollapseAwareEvalGate  (held-out real: mean delta + diversity + tail)
      → GeneralistAdapterRollout (promote → replicate ×4)

The trainer and the held-out eval are injected, so the whole flywheel turns once
in a test without a GPU. This is the orchestration spine; it owns the *order*
and the guardrail wiring, not the heavy lifting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from turing.learning.eval_set.collapse_gate import CollapseGateError
from turing.learning.trainer.job import TrainingJob

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from turing.coordinator.flywheel.generalist import GeneralistAdapterRollout, GeneralistFleet
    from turing.coordinator.flywheel.morning_curation import SFTCandidate
    from turing.coordinator.promotion.rollout import RolloutCoordinator
    from turing.learning.eval_set.collapse_gate import CollapseAwareEvalGate, EvalRun
    from turing.learning.trainer.lora_recipe import LoraRecipe, RoundCapGuard
    from turing.learning.trainer.publisher import TrainerPublisher
    from turing.learning.trainer.runner import TrainingResult
    from turing.learning.trainer.sft_dataset_builder import SFTDatasetBuilder


class _Trainer(Protocol):
    def train(self, job: TrainingJob, artifact_dir: Path) -> TrainingResult: ...


class CycleRejectedError(RuntimeError):
    """Raised when the eval gate refuses the cycle's adapter (no promotion)."""

    def __init__(self, *, reason: str) -> None:
        super().__init__(f"training cycle rejected at the eval gate: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class CycleReport:
    """What one flywheel turn produced, for the operator's record."""

    round_number: int
    dataset_examples: int
    dataset_sha256: str
    adapter_name: str
    adapter_version: str
    promoted: bool
    mean_delta: float
    rollout: RolloutCoordinator | None = None


class FlywheelTrainingCycle:
    """Turns the training flywheel once, composing the §4 guardrails in order."""

    def __init__(
        self,
        *,
        dataset_builder: SFTDatasetBuilder,
        recipe: LoraRecipe,
        round_guard: RoundCapGuard,
        trainer: _Trainer,
        publisher: TrainerPublisher,
        rollout: GeneralistAdapterRollout,
        fleet: GeneralistFleet,
        eval_gate: CollapseAwareEvalGate,
        held_out_eval: Callable[[str], tuple[EvalRun, EvalRun]],
        artifact_dir: Path,
    ) -> None:
        self._builder = dataset_builder
        self._recipe = recipe
        self._guard = round_guard
        self._trainer = trainer
        self._publisher = publisher
        self._rollout = rollout
        self._fleet = fleet
        self._gate = eval_gate
        self._held_out_eval = held_out_eval
        self._artifact_dir = artifact_dir

    def run_cycle(
        self,
        *,
        base_model: str,
        curated: Sequence[SFTCandidate],
        version: str,
    ) -> CycleReport:
        """Accumulate → train-from-base → eval-gate → promote → replicate ×4.

        Raises :class:`~turing.learning.trainer.lora_recipe.RoundCapExceededError`
        if the specialty has exhausted its 1-3-round budget, and
        :class:`CycleRejectedError` if the collapse-aware gate refuses the
        candidate (mean delta too small, diversity collapse, or tail collapse).
        """
        specialty = self._fleet.specialty
        # 1-3 useful rounds, then re-evaluate (raises if the cap is hit).
        round_number = self._guard.check_and_increment(specialty)

        # Accumulate the new curated pairs onto the frozen-seed corpus, then
        # build the reasoning-bearing dataset (accumulate-never-replace).
        self._builder.accumulate(curated)
        dataset_path = self._artifact_dir / f"dataset-{version}.jsonl"
        dataset = self._builder.build(out_path=dataset_path)

        # From-base LoRA on all linear layers, few epochs (recipe-pinned).
        job = TrainingJob(
            job_id=f"{specialty}-{version}",
            dataset_url=f"file://{dataset.out_path}",
            dataset_sha256=dataset.dataset_sha256,
            base_model=base_model,
            method="sft",
            hyperparameters=self._recipe.to_hyperparameters(),
        )
        result = self._trainer.train(job, self._artifact_dir)

        # Sign + register (verify on load → STAGED).
        manifest = self._publisher.publish(job, result, version=version)
        self._rollout.registry.register(manifest, result.adapter_path.read_bytes())

        # Collapse-aware eval gate on held-out *real* data.
        baseline_run, candidate_run = self._held_out_eval(version)
        try:
            decision = self._gate.check(baseline=baseline_run, candidate=candidate_run)
        except CollapseGateError as exc:
            # The adapter stays STAGED in the registry (audit trail) but never
            # promotes to the fleet — collapse / no-improvement blocks rollout.
            raise CycleRejectedError(reason=str(exc)) from exc

        # Promote → replicate ×4 via the existing rollout path.
        coordinator = self._rollout.replicate(
            name=manifest.name,
            version=version,
            baseline_score=baseline_run.mean_score,
            canary_eval_score=candidate_run.mean_score,
        )
        return CycleReport(
            round_number=round_number,
            dataset_examples=dataset.example_count,
            dataset_sha256=dataset.dataset_sha256,
            adapter_name=manifest.name,
            adapter_version=version,
            promoted=True,
            mean_delta=decision.mean_delta,
            rollout=coordinator,
        )
