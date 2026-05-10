"""TrainerConfig — runtime config for the systemd-run trainer unit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class TrainerConfig:
    """Configuration consumed by ``TrainerPullAgent``.

    ``facility_name`` is part of the queue subject (``training.queue.<name>``)
    so the same NATS server can serve many trainer facilities without crosstalk.
    """

    facility_name: str
    artifact_dir: Path
    queue_subject_template: str = "training.queue.{facility}"
    events_subject_template: str = "training.events.{job_id}"

    def queue_subject(self) -> str:
        return self.queue_subject_template.format(facility=self.facility_name)

    def events_subject(self, job_id: str) -> str:
        return self.events_subject_template.format(job_id=job_id)
