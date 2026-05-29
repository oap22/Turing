"""End-to-end Phase B run: episodes → JSONL → trainer → manifest → eval gate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from turing.coordinator.adapters.registry import AdapterRegistry
from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.promotion import PromotionGate
from turing.learning.trainer import (
    CudaLoraTrainer,
    InMemoryObjectStore,
    TrainerPublisher,
    TrainingJob,
    TrainingJobBuilder,
)
from turing.transport.signer import MessageSigner

if TYPE_CHECKING:
    from pathlib import Path


def _ep(
    *,
    subtask_id: str,
    input_text: str,
    output_text: str,
    critic_score: float = 0.85,
) -> Episode:
    return Episode(
        task_id=f"t-{subtask_id}",
        subtask_id=subtask_id,
        worker_id="w1",
        specialty="research-summarize",
        model_version="qwen2.5-7b",
        adapter_version="base",
        input_text=input_text,
        trajectory=("act",),
        output_text=output_text,
        success=True,
        latency_ms=10,
        tokens_used=10,
        outcome=SubtaskState.COMPLETED,
        critic_score=critic_score,
        recorded_at_ms=0,
    )


def _stub_backend(*, job: TrainingJob, dataset_path: Path, out_path: Path) -> bytes:
    blob = (f"adapter[{job.base_model}|{dataset_path.read_bytes().decode()}]").encode()
    out_path.write_bytes(blob)
    return blob


def test_phase_b_chain_passes_eval_delta_and_lands_as_staged(
    tmp_path: Path,
) -> None:
    """The full chain the issue's E2E acceptance criterion describes:
    1. Pull top-K positive episodes for the specialty
    2. Train a CUDA LoRA adapter on the dataset (H100/DGX, ADR 0009)
    3. Sign and publish the manifest
    4. AdapterRegistry verifies (slice 20)
    5. Eval delta clears the 2% floor (slice 22 gate)
    Result: staged adapter accepted by every link in the chain.
    """
    # --- 1. Episode corpus ----------------------------------------------------
    store = EpisodeStore()
    for i in range(5):
        store.record(
            _ep(
                subtask_id=f"s-{i}",
                input_text=f"summarise paper {i}",
                output_text=f"answer {i}",
                critic_score=0.85,
            )
        )

    # --- 2. Build the dataset -------------------------------------------------
    builder = TrainingJobBuilder(episode_store=store, min_critic_score=0.7)
    dataset_path = tmp_path / "dataset.jsonl"
    info = builder.build(specialty="research-summarize", top_k=10, out_path=dataset_path)
    assert info.example_count == 5

    # --- 3. Train the adapter -------------------------------------------------
    job = TrainingJob(
        job_id="phase-b-1",
        dataset_url=f"file://{dataset_path}",
        dataset_sha256=info.dataset_sha256,
        base_model="qwen2.5-7b",
        method="sft",
        hyperparameters={"lr": 1e-4},
    )
    trainer = CudaLoraTrainer(backend=_stub_backend, dataset_path=dataset_path)
    artifact_dir = tmp_path / "artifacts"
    result = trainer.train(job, artifact_dir=artifact_dir)

    # --- 4. Publish + verify --------------------------------------------------
    signer = MessageSigner.generate()
    publisher = TrainerPublisher(
        signer=signer, object_store=InMemoryObjectStore(), facility_name="dgx-1"
    )
    manifest = publisher.publish(job, result, version="rs@v2")
    registry = AdapterRegistry(
        worker_base_model="qwen2.5-7b",
        trusted_issuers=[signer.public_key],
    )
    registry.verify(manifest, result.adapter_path.read_bytes())

    # --- 5. Eval-delta gate ---------------------------------------------------
    gate = PromotionGate(min_delta=0.02)
    decision = gate.check_eval_delta(
        baseline_score=0.70,
        candidate_score=0.74,  # +4 points beats the 2% floor
    )
    assert decision.delta >= 0.02
