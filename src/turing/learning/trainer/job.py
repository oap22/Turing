"""TrainingJob — the JSON message published on ``training.queue.<facility>``.

The trainer is a pull-only client; it never accepts arbitrary commands. A
``TrainingJob`` is the only thing it acts on, so this parser is paranoid
about shape and rejects anything it can't fully validate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

ALLOWED_METHODS = ("sft", "dpo")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class TrainingJobValidationError(ValueError):
    """Raised when a training-job payload fails schema validation."""


@dataclass(frozen=True)
class TrainingJob:
    job_id: str
    dataset_url: str
    dataset_sha256: str
    base_model: str
    method: Literal["sft", "dpo"]
    hyperparameters: dict[str, Any]

    @classmethod
    def from_bytes(cls, payload: bytes) -> "TrainingJob":
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TrainingJobValidationError(f"invalid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise TrainingJobValidationError("training job must be a JSON object")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TrainingJob":
        for field in ("job_id", "dataset_url", "dataset_sha256", "base_model", "method", "hyperparameters"):
            if field not in raw:
                raise TrainingJobValidationError(f"missing required field {field!r}")
        if raw["method"] not in ALLOWED_METHODS:
            raise TrainingJobValidationError(
                f"method must be one of {ALLOWED_METHODS}, got {raw['method']!r}"
            )
        if not _SHA256_RE.match(str(raw["dataset_sha256"])):
            raise TrainingJobValidationError(
                f"dataset_sha256 must be 64 hex chars, got {raw['dataset_sha256']!r}"
            )
        if not isinstance(raw["hyperparameters"], dict):
            raise TrainingJobValidationError("hyperparameters must be an object")
        return cls(
            job_id=str(raw["job_id"]),
            dataset_url=str(raw["dataset_url"]),
            dataset_sha256=str(raw["dataset_sha256"]).lower(),
            base_model=str(raw["base_model"]),
            method=raw["method"],
            hyperparameters=dict(raw["hyperparameters"]),
        )
