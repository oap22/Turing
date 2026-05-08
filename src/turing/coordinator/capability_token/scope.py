"""CapabilityScope — the signed shell-execution authorisation (ADR 0003 v1).

Per ADR 0003 §3, ``cwd`` is **not** a scope field — the gate resolves the
workspace from ``(task_id, subtask_id)``. The scope carries identity
(version + ids), the policy regex, and lifetime fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

SCOPE_VERSION = 1


@dataclass(frozen=True)
class CapabilityScope:
    subtask_id: str
    task_id: str
    allowed_commands_regex: str
    timeout_s: int
    expires_at_ms: int
    issued_at_ms: int
    version: int = SCOPE_VERSION

    def signing_bytes(self) -> bytes:
        """Stable canonical encoding for signing/verification."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "subtask_id": self.subtask_id,
            "task_id": self.task_id,
            "allowed_commands_regex": self.allowed_commands_regex,
            "timeout_s": self.timeout_s,
            "expires_at_ms": self.expires_at_ms,
            "issued_at_ms": self.issued_at_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CapabilityScope:
        return cls(
            version=d.get("version", SCOPE_VERSION),
            subtask_id=d["subtask_id"],
            task_id=d["task_id"],
            allowed_commands_regex=d["allowed_commands_regex"],
            timeout_s=d["timeout_s"],
            expires_at_ms=d["expires_at_ms"],
            issued_at_ms=d["issued_at_ms"],
        )
