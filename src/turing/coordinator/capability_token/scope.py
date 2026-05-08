"""CapabilityScope — the signed shell-execution authorisation."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityScope:
    subtask_id: str
    allowed_commands_regex: str
    cwd: str
    timeout_s: int
    expires_at_ms: int

    def signing_bytes(self) -> bytes:
        """Stable canonical encoding for signing/verification."""
        return json.dumps(
            {
                "subtask_id": self.subtask_id,
                "allowed_commands_regex": self.allowed_commands_regex,
                "cwd": self.cwd,
                "timeout_s": self.timeout_s,
                "expires_at_ms": self.expires_at_ms,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
