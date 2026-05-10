"""CapabilityManifest — versioned worker self-description.

Each worker advertises this at registration and refreshes it on heartbeat.
The schema is versioned: a worker built against an older manifest format is
rejected on register with a `ManifestVersionError`, never silently dispatched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CURRENT_MANIFEST_VERSION = 1


class ManifestVersionError(Exception):
    """Raised when an inbound manifest's schema_version doesn't match ours."""


@dataclass(frozen=True)
class CapabilityManifest:
    worker_id: str
    specialties: tuple[str, ...]
    base_model: str
    adapters: tuple[str, ...]
    tools: tuple[str, ...]
    hardware: str
    max_concurrent: int
    eval_score: float
    public_key: bytes
    schema_version: int = field(default=CURRENT_MANIFEST_VERSION)

    def __post_init__(self) -> None:
        if not self.specialties:
            raise ValueError("manifest requires at least one specialty")
        if self.max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "specialties": list(self.specialties),
            "base_model": self.base_model,
            "adapters": list(self.adapters),
            "tools": list(self.tools),
            "hardware": self.hardware,
            "max_concurrent": self.max_concurrent,
            "eval_score": self.eval_score,
            "public_key": self.public_key.hex(),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CapabilityManifest:
        version = raw.get("schema_version")
        if version != CURRENT_MANIFEST_VERSION:
            raise ManifestVersionError(
                f"manifest schema_version {version!r} does not match {CURRENT_MANIFEST_VERSION}"
            )
        return cls(
            worker_id=raw["worker_id"],
            specialties=tuple(raw["specialties"]),
            base_model=raw["base_model"],
            adapters=tuple(raw["adapters"]),
            tools=tuple(raw["tools"]),
            hardware=raw["hardware"],
            max_concurrent=raw["max_concurrent"],
            eval_score=raw["eval_score"],
            public_key=bytes.fromhex(raw["public_key"]),
            schema_version=version,
        )
