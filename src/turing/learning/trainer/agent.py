"""TrainerPullAgent — the long-running trainer subscriber.

Subscribes to ``training.queue.<facility>``, runs each job through the
configured ``Trainer``, publishes a signed manifest via ``TrainerPublisher``,
and streams ``started`` / ``step`` / ``complete`` / ``failed`` events on
``training.events.<job_id>``. The agent never accepts inbound connections —
it only publishes and subscribes through the supplied ``Bus``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

from turing.learning.trainer.job import TrainingJob, TrainingJobValidationError

if TYPE_CHECKING:
    from turing.learning.trainer.config import TrainerConfig
    from turing.learning.trainer.publisher import TrainerPublisher
    from turing.learning.trainer.runner import Trainer
    from turing.transport.bus import Bus

logger = structlog.get_logger(__name__)


class TrainerPullAgent:
    def __init__(
        self,
        *,
        config: TrainerConfig,
        bus: Bus,
        trainer: Trainer,
        publisher: TrainerPublisher,
    ) -> None:
        self._config = config
        self._bus = bus
        self._trainer = trainer
        self._publisher = publisher

    async def start(self) -> None:
        await self._bus.subscribe(self._config.queue_subject(), self._on_message)
        logger.info(
            "trainer_pull_agent_started",
            facility=self._config.facility_name,
            subject=self._config.queue_subject(),
        )

    async def _on_message(self, payload: bytes) -> None:
        try:
            job = TrainingJob.from_bytes(payload)
        except TrainingJobValidationError:
            logger.warning("trainer_invalid_job_payload", exc_info=True)
            return

        events_subject = self._config.events_subject(job.job_id)
        await self._emit(events_subject, {"kind": "started", "job_id": job.job_id})

        try:
            steps: list[dict[str, object]] = []
            result = self._trainer.train(
                job,
                self._config.artifact_dir,
                emit=steps.append,
            )
            for step in steps:
                await self._emit(events_subject, step)
            manifest = self._publisher.publish(job, result, version=job.job_id)
            await self._emit(
                events_subject,
                {
                    "kind": "complete",
                    "job_id": job.job_id,
                    "manifest": manifest.to_dict(),
                },
            )
        except Exception as exc:
            logger.warning("trainer_job_failed", job_id=job.job_id, exc_info=True)
            await self._emit(
                events_subject,
                {"kind": "failed", "job_id": job.job_id, "error": str(exc)},
            )

    async def _emit(self, subject: str, event: dict[str, object]) -> None:
        await self._bus.publish(subject, json.dumps(event).encode("utf-8"))
