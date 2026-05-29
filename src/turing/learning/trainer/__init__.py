"""turing-trainer: pull-only systemd agent for the H100/DGX training facility.

Subscribes to ``training.queue.<facility>`` on the coordinator's NATS, runs
LoRA SFT/DPO jobs, and publishes signed adapters back to the cluster. Refuses
any inbound connection. See ``deploy/turing-trainer.service``.
"""

from __future__ import annotations

from turing.learning.trainer.agent import TrainerPullAgent
from turing.learning.trainer.config import TrainerConfig
from turing.learning.trainer.cuda_lora_trainer import CudaLoraTrainer
from turing.learning.trainer.dial_guard import (
    InboundConnectionRefusedError,
    PullOnlySocketGuard,
)
from turing.learning.trainer.dpo_dataset_builder import (
    DPODatasetBuilder,
    DPODatasetInfo,
)
from turing.learning.trainer.job import TrainingJob, TrainingJobValidationError
from turing.learning.trainer.job_builder import DatasetInfo, TrainingJobBuilder
from turing.learning.trainer.object_store import InMemoryObjectStore, ObjectStore
from turing.learning.trainer.publisher import TrainerPublisher
from turing.learning.trainer.runner import (
    RealLoraTrainer,
    StubTrainer,
    Trainer,
    TrainingResult,
)
from turing.learning.trainer.sft_dataset_builder import (
    DatasetReplaceError,
    SFTDatasetBuilder,
    SFTDatasetInfo,
    SFTExample,
    candidates_from_rows,
)
from turing.learning.trainer.teacher_ab import (
    NoTeacherArmsError,
    TeacherABHarness,
    TeacherABReport,
    TeacherArmResult,
    TeacherCandidate,
)
from turing.learning.trainer.torch_dpo_trainer import TorchDPOTrainer

__all__ = [
    "CudaLoraTrainer",
    "DPODatasetBuilder",
    "DPODatasetInfo",
    "DatasetInfo",
    "DatasetReplaceError",
    "InMemoryObjectStore",
    "InboundConnectionRefusedError",
    "NoTeacherArmsError",
    "ObjectStore",
    "PullOnlySocketGuard",
    "RealLoraTrainer",
    "SFTDatasetBuilder",
    "SFTDatasetInfo",
    "SFTExample",
    "StubTrainer",
    "TeacherABHarness",
    "TeacherABReport",
    "TeacherArmResult",
    "TeacherCandidate",
    "TorchDPOTrainer",
    "Trainer",
    "TrainerConfig",
    "TrainerPublisher",
    "TrainerPullAgent",
    "TrainingJob",
    "TrainingJobBuilder",
    "TrainingJobValidationError",
    "TrainingResult",
    "candidates_from_rows",
]
