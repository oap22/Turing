"""CUDA LoRA SFT trainer (issue #260, ADR 0009): produces an adapter the
registry accepts.

The real trainer uses peft + CUDA torch on the H100/DGX. We can't import that
stack in CI (no GPU), so the backend is injected: the test drives the wiring
with a stub and asserts the produced blob round-trips through the publisher and
AdapterRegistry verification.

The ``CudaLoraTrainer`` class itself is the wiring; the real backend imports
peft/torch lazily so the coordinator and Jetson workers can still import the
module without a GPU.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from turing.coordinator.adapters.registry import AdapterRegistry
from turing.learning.trainer import (
    CudaLoraTrainer,
    InMemoryObjectStore,
    TrainerPublisher,
    TrainingJob,
)
from turing.transport.signer import MessageSigner

if TYPE_CHECKING:
    from pathlib import Path


def _job(*, dataset_path: Path) -> TrainingJob:
    return TrainingJob(
        job_id="cuda-lora-1",
        dataset_url=f"file://{dataset_path}",
        dataset_sha256="0" * 64,
        base_model="qwen2.5-7b",
        method="sft",
        hyperparameters={"lr": 1e-4, "rank": 16},
    )


def _stub_backend(*, job: TrainingJob, dataset_path: Path, out_path: Path) -> bytes:
    """Stand-in for the real CUDA LoRA SFT path."""
    payload = (
        f"cuda-lora({job.base_model}|{job.method}|"
        f"{dataset_path.read_bytes().decode('utf-8', errors='replace')})"
    ).encode()
    out_path.write_bytes(payload)
    return payload


def test_cuda_lora_trainer_writes_adapter_to_artifact_dir(tmp_path: Path) -> None:
    dataset = tmp_path / "ds.jsonl"
    dataset.write_text(json.dumps({"input": "i", "output": "o"}) + "\n", encoding="utf-8")
    artifact_dir = tmp_path / "out"

    trainer = CudaLoraTrainer(backend=_stub_backend, dataset_path=dataset)
    result = trainer.train(_job(dataset_path=dataset), artifact_dir=artifact_dir)

    assert result.adapter_path.exists()
    assert result.adapter_path.parent == artifact_dir
    assert result.adapter_path.name.endswith(".adapter.safetensors")
    assert result.sha256 == hashlib.sha256(result.adapter_path.read_bytes()).hexdigest()


def test_cuda_lora_trainer_emits_started_and_complete(tmp_path: Path) -> None:
    dataset = tmp_path / "ds.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    events: list[dict] = []

    trainer = CudaLoraTrainer(backend=_stub_backend, dataset_path=dataset)
    trainer.train(_job(dataset_path=dataset), artifact_dir=tmp_path / "out", emit=events.append)

    kinds = [e["kind"] for e in events]
    assert kinds == ["started", "complete"]


def test_publisher_signs_manifest_registry_accepts(tmp_path: Path) -> None:
    """SHA256 + Ed25519 verification (ADR 0007/0009) holds for a CUDA adapter."""
    dataset = tmp_path / "ds.jsonl"
    dataset.write_text(json.dumps({"input": "i", "output": "o"}) + "\n", encoding="utf-8")

    trainer = CudaLoraTrainer(backend=_stub_backend, dataset_path=dataset)
    result = trainer.train(_job(dataset_path=dataset), artifact_dir=tmp_path / "out")

    signer = MessageSigner.generate()
    publisher = TrainerPublisher(
        signer=signer, object_store=InMemoryObjectStore(), facility_name="dgx-1"
    )
    manifest = publisher.publish(_job(dataset_path=dataset), result, version="v1")

    registry = AdapterRegistry(
        worker_base_model="qwen2.5-7b",
        trusted_issuers=[signer.public_key],
    )
    registry.verify(manifest, result.adapter_path.read_bytes())  # does not raise


def test_real_backend_constructs_without_gpu(tmp_path: Path) -> None:
    """Constructing with backend=None (the real CUDA path) must not fail on a
    GPU-less host — the peft/torch import is deferred until train() runs."""
    dataset = tmp_path / "ds.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    # No train() call: the heavy import only happens inside the backend.
    trainer = CudaLoraTrainer(backend=None, dataset_path=dataset)
    assert trainer is not None


def test_no_mlx_dependency_remains() -> None:
    """ADR 0009: MLX is removed from the fleet; the trainer package must not
    export or import an MLX trainer."""
    import turing.learning.trainer as trainer_pkg

    assert not hasattr(trainer_pkg, "MLXLoraTrainer")
    assert "MLXLoraTrainer" not in trainer_pkg.__all__
    assert "CudaLoraTrainer" in trainer_pkg.__all__
