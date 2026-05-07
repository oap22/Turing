"""MeshMessage envelope and replay-protection window.

`MeshMessage` is the typed envelope every coordinator/worker exchange wraps.
It serialises to a deterministic byte string so signing covers every field.
`ReplayWindow` enforces request-id deduplication within a configurable TTL,
mitigating message replay even if a signing key briefly leaks.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class ReplayError(Exception):
    """Raised when a message is a replay or falls outside the freshness window."""


@dataclass(frozen=True)
class MeshMessage:
    request_id: str
    sender_id: str
    subject: str
    payload: bytes
    timestamp_ms: int

    def to_bytes(self) -> bytes:
        d = asdict(self)
        d["payload"] = self.payload.hex()
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> MeshMessage:
        d = json.loads(raw.decode("utf-8"))
        return cls(
            request_id=d["request_id"],
            sender_id=d["sender_id"],
            subject=d["subject"],
            payload=bytes.fromhex(d["payload"]),
            timestamp_ms=d["timestamp_ms"],
        )


def _default_clock_ms() -> int:
    return int(time.time() * 1000)


class ReplayWindow:
    """Reject duplicate `request_id`s and stale messages within a TTL window."""

    def __init__(
        self,
        *,
        ttl_ms: int,
        now_ms: Callable[[], int] = _default_clock_ms,
    ) -> None:
        if ttl_ms <= 0:
            raise ValueError("ttl_ms must be positive")
        self._ttl_ms = ttl_ms
        self._now_ms = now_ms
        self._seen: dict[str, int] = {}

    def observe(self, *, request_id: str, timestamp_ms: int) -> None:
        now = self._now_ms()
        self._evict(now)

        if now - timestamp_ms > self._ttl_ms:
            raise ReplayError(
                f"message timestamp {timestamp_ms} is older than ttl {self._ttl_ms}ms"
            )

        if request_id in self._seen:
            raise ReplayError(f"replay of request_id={request_id!r}")

        self._seen[request_id] = timestamp_ms

    def _evict(self, now: int) -> None:
        cutoff = now - self._ttl_ms
        expired = [rid for rid, ts in self._seen.items() if ts < cutoff]
        for rid in expired:
            del self._seen[rid]
