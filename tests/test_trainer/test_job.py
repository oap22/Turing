"""Tests for TrainingJob: the JSON message the coordinator publishes."""

from __future__ import annotations

import json

import pytest

from turing.learning.trainer.job import TrainingJob, TrainingJobValidationError


class TestParse:
    def test_parses_minimum_required_fields(self) -> None:
        raw = json.dumps(
            {
                "job_id": "j-001",
                "dataset_url": "s3://bucket/datasets/sft.tar.gz",
                "dataset_sha256": "a" * 64,
                "base_model": "qwen2.5-7b",
                "method": "sft",
                "hyperparameters": {},
            }
        ).encode("utf-8")
        job = TrainingJob.from_bytes(raw)
        assert job.job_id == "j-001"
        assert job.method == "sft"
        assert job.base_model == "qwen2.5-7b"
        assert job.hyperparameters == {}

    def test_method_must_be_sft_or_dpo(self) -> None:
        raw = json.dumps(
            {
                "job_id": "j-002",
                "dataset_url": "s3://x/y",
                "dataset_sha256": "b" * 64,
                "base_model": "llama-3-8b",
                "method": "garbage",
                "hyperparameters": {},
            }
        ).encode("utf-8")
        with pytest.raises(TrainingJobValidationError, match="method"):
            TrainingJob.from_bytes(raw)

    def test_missing_required_field_raises(self) -> None:
        raw = json.dumps({"job_id": "j-003"}).encode("utf-8")
        with pytest.raises(TrainingJobValidationError):
            TrainingJob.from_bytes(raw)

    def test_dataset_sha256_must_be_64_hex(self) -> None:
        raw = json.dumps(
            {
                "job_id": "j-004",
                "dataset_url": "s3://x/y",
                "dataset_sha256": "short",
                "base_model": "qwen2.5-7b",
                "method": "sft",
                "hyperparameters": {},
            }
        ).encode("utf-8")
        with pytest.raises(TrainingJobValidationError, match="sha256"):
            TrainingJob.from_bytes(raw)

    def test_invalid_json_raises_validation_error(self) -> None:
        with pytest.raises(TrainingJobValidationError):
            TrainingJob.from_bytes(b"not json{")
