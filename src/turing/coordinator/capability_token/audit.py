"""Capability gate audit log (ADR 0003 §9).

In-memory append-only list of every gate decision. The schema matches the
issue body's nine columns; production wires this to a SQLite projection
later (out of scope for #97).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GateAuditRow:
    ts_ms: int
    subtask_id: str
    worker_id: str
    request_id: str
    tool: str
    command: str
    outcome: str  # DROPPED | DENIED | ALLOWED
    reason: str
    exit_code: int | None
    duration_ms: int
    stdout_truncated_bytes: int
    stderr_truncated_bytes: int
    token_fingerprint: str


class GateAuditLog:
    def __init__(self) -> None:
        self._rows: list[GateAuditRow] = []

    def append(self, row: GateAuditRow) -> None:
        self._rows.append(row)

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def rows(self) -> list[GateAuditRow]:
        return list(self._rows)
