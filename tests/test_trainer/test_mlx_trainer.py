"""MLX LoRA SFT trainer: produces an adapter the registry accepts.

The real trainer uses Apple's `mlx_lm` SFT path. We can't import it on
non-macOS hosts, so the tests use a stub backend that mimics the contract:
take a JSONL dataset path, write a checkpoint blob, and return a
TrainingResult that the existing TrainerPublisher (slice 23) signs into a
manifest the AdapterRegistry verifies.

The MLXLoraTrainer class itself is the wiring; the real backend imports
mlx_lm lazily so non-macOS workers can still import the module.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from turing.coordinator.adapters.registry import AdapterRegistry
from turing.learning.trainer import (
    InMemoryObjectStore,
    MLXLoraTrainer,
    TrainerPublisher,
    TrainingJob,
)
from turing.transport.signer import MessageSigner

if TYPE_CHECKING:
    from pathlib import Path


def _job(*, dataset_path: Path) -> TrainingJob:
    return TrainingJob(
        job_id="job-1",
        dataset_url=f"file://{dataset_path}",
        dataset_sha256="0" * 64,
        base_model="qwen2.5-7b",
        method="sft",
        hyperparameters={"lr": 1e-4, "epochs": 1},
    )


def _stub_backend(*, job: TrainingJob, dataset_path: Path, out_path: Path) -> bytes:
    """Stand-in for the real mlx_lm SFT path.

    Writes a deterministic adapter blob keyed by the dataset content so
    the manifest's sha256 is meaningful in tests."""
    payload = (
        f"mlx-lora({job.base_model}|{job.method}|"
        f"{dataset_path.read_bytes().decode('utf-8', errors='replace')})"
    ).encode()
    out_path.write_bytes(payload)
    return payload


def test_mlx_trainer_writes_adapter_to_artifact_dir(tmp_path: Path) -> None:
    dataset = tmp_path / "ds.jsonl"
    dataset.write_text(json.dumps({"input": "i", "output": "o"}) + "\n", encoding="utf-8")
    artifact_dir = tmp_path / "out"
    trainer = MLXLoraTrainer(backend=_stub_backend, dataset_path=dataset)
    result = trainer.train(_job(dataset_path=dataset), artifact_dir=artifact_dir)
    assert result.adapter_path.exists()
    assert result.adapter_path.parent == artifact_dir
    # The sha256 in the result actually corresponds to the bytes on disk.
    assert result.sha256 == hashlib.sha256(result.adapter_path.read_bytes()).hexdigest()


def test_publisher_signs_manifest_registry_accepts(tmp_path: Path) -> None:
    dataset = tmp_path / "ds.jsonl"
    dataset.write_text(json.dumps({"input": "i", "output": "o"}) + "\n", encoding="utf-8")
    artifact_dir = tmp_path / "out"
    trainer = MLXLoraTrainer(backend=_stub_backend, dataset_path=dataset)
    result = trainer.train(_job(dataset_path=dataset), artifact_dir=artifact_dir)

    signer = MessageSigner.generate()
    publisher = TrainerPublisher(
        signer=signer, object_store=InMemoryObjectStore(), facility_name="mbp"
    )
    manifest = publisher.publish(_job(dataset_path=dataset), result, version="v1")

    registry = AdapterRegistry(
        worker_base_model="qwen2.5-7b",
        trusted_issuers=[signer.public_key],
    )
    blob = result.adapter_path.read_bytes()
    # Verify the manifest+blob round-trip.
    registry.verify(manifest, blob)


def test_real_backend_imports_mlx_lazily(tmp_path: Path) -> None:
    """Constructing MLXLoraTrainer with backend=None must not fail on
    non-macOS hosts; the import only happens when train() runs."""
    dataset = tmp_path / "ds.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    # Backend=None means "use the real MLX path"; we don't call train()
    # here, so no import should happen.
    trainer = MLXLoraTrainer(backend=None, dataset_path=dataset)
    assert trainer is not None
