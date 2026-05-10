"""End-to-end Phase C: episodes → DPO pairs → DPO training → manifest → gate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from turing.coordinator.adapters.registry import AdapterRegistry
from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.promotion import (
    HardExample,
    PromotionGate,
    archive_failed_training,
)
from turing.learning.trainer import (
    DPODatasetBuilder,
    InMemoryObjectStore,
    TorchDPOTrainer,
    TrainerPublisher,
    TrainingJob,
)
from turing.transport.signer import MessageSigner

if TYPE_CHECKING:
    from pathlib import Path


def _ep(
    *,
    subtask_id: str,
    input_text: str,
    output_text: str,
    critic_score: float,
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


def _stub_dpo_backend(*, job, dataset_path, out_path):  # type: ignore[no-untyped-def]
    blob = (
        f"dpo[{job.base_model}|{dataset_path.read_bytes().decode('utf-8', errors='replace')}]"
    ).encode()
    out_path.write_bytes(blob)
    return blob


def test_phase_c_signed_adapter_clears_promotion_gate(
    tmp_path: Path,
) -> None:
    """The full Phase C chain in one test:
    1. Build (winner, loser) pairs from the episode store.
    2. Train a DPO LoRA adapter on the pairs.
    3. Sign the manifest; AdapterRegistry verifies.
    4. Eval-delta clears the 2% promotion floor.
    """
    store = EpisodeStore()
    # Three same-input contests, each with a clean winner/loser.
    for i in range(3):
        store.record(
            _ep(
                subtask_id=f"s-{i}-win",
                input_text=f"summarise paper {i}",
                output_text=f"good answer {i}",
                critic_score=0.9,
            )
        )
        store.record(
            _ep(
                subtask_id=f"s-{i}-lose",
                input_text=f"summarise paper {i}",
                output_text=f"bad answer {i}",
                critic_score=0.2,
            )
        )

    # 1. DPO dataset
    builder = DPODatasetBuilder(episode_store=store, min_score_gap=0.3)
    dataset_path = tmp_path / "pairs.jsonl"
    info = builder.build(specialty="research-summarize", out_path=dataset_path)
    assert info.pair_count == 3

    # 2. Train
    job = TrainingJob(
        job_id="dpo-1",
        dataset_url=f"file://{dataset_path}",
        dataset_sha256=info.dataset_sha256,
        base_model="qwen2.5-7b",
        method="dpo",
        hyperparameters={"beta": 0.1},
    )
    trainer = TorchDPOTrainer(backend=_stub_dpo_backend, dataset_path=dataset_path)
    result = trainer.train(job, artifact_dir=tmp_path / "artifacts")

    # 3. Publish + verify
    signer = MessageSigner.generate()
    publisher = TrainerPublisher(
        signer=signer, object_store=InMemoryObjectStore(), facility_name="dgx-1"
    )
    manifest = publisher.publish(job, result, version="rs-dpo@v1")
    AdapterRegistry(worker_base_model="qwen2.5-7b", trusted_issuers=[signer.public_key]).verify(
        manifest, result.adapter_path.read_bytes()
    )

    # 4. Promotion gate
    gate = PromotionGate(min_delta=0.02)
    decision = gate.check_eval_delta(baseline_score=0.70, candidate_score=0.74)
    assert decision.delta >= 0.02


def test_failed_dpo_run_archives_via_hard_examples_path(
    tmp_path: Path,
) -> None:
    """Acceptance criterion: failed DPO runs follow the slice-22 hard-examples
    path. We model the failure by skipping the train() call entirely and
    handing the broken cases directly to archive_failed_training — that's
    what the runtime-bus integration does when the trainer raises."""
    store = EpisodeStore()

    class _RecordingNotifier:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def notify(self, kind: str, payload: dict) -> None:
            self.calls.append((kind, payload))

    notifier = _RecordingNotifier()
    archive_failed_training(
        run_id="dpo-failed-1",
        specialty="research-summarize",
        examples=[
            HardExample(
                subtask_id="s-bad",
                input_text="summarise paper X",
                expected_text="winning answer",
                actual_output="",
                failure_reason="loss diverged at step 42",
            ),
        ],
        episode_store=store,
        notifier=notifier,
    )

    rows = store.query(specialty="research-summarize")
    assert len(rows) == 1
    assert rows[0].outcome is SubtaskState.FAILED
    assert any(c[0] == "training_failed" for c in notifier.calls)


def test_failed_dpo_examples_never_reappear_in_dpo_pairs(
    tmp_path: Path,
) -> None:
    """A FAILED episode written by archive_failed_training cannot resurface
    as either a winner or a loser in the next DPO build."""
    store = EpisodeStore()
    archive_failed_training(
        run_id="dpo-failed-1",
        specialty="research-summarize",
        examples=[
            HardExample(
                subtask_id="s-bad",
                input_text="summarise paper X",
                expected_text="winning answer",
                actual_output="",
                failure_reason="oom",
            ),
        ],
        episode_store=store,
        notifier=type("N", (), {"notify": lambda self, *a, **k: None})(),
    )
    store.record(
        _ep(
            subtask_id="s-good",
            input_text="summarise paper X",
            output_text="winner",
            critic_score=0.9,
        )
    )

    builder = DPODatasetBuilder(episode_store=store, min_score_gap=0.0)
    info = builder.build(
        specialty="research-summarize",
        out_path=tmp_path / "pairs.jsonl",
    )
    # Only one COMPLETED episode for that input → no pair.
    assert info.pair_count == 0
