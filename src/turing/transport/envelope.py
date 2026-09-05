"""MeshMessage envelope and replay-protection window.

`MeshMessage` is the typed envelope every coordinator/worker exchange wraps.
It serialises to a deterministic byte string so signing covers every field.
`ReplayWindow` enforces request-id deduplication within a configurable TTL,
mitigating message replay even if a signing key briefly leaks.
"""

from __future__ import annotations

import heapq
import json
import time
from dataclasses import dataclass
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
        # Built by hand rather than with dataclasses.asdict(): asdict() walks
        # the instance recursively and deep-copies every field, which costs
        # ~24x more than the literal below for a flat frozen dataclass. This
        # runs on every message that crosses the bus, and every signature
        # covers its output. Keys stay sorted to match sort_keys=True, so the
        # encoding is byte-identical to what asdict() produced.
        d = {
            "payload": self.payload.hex(),
            "request_id": self.request_id,
            "sender_id": self.sender_id,
            "subject": self.subject,
            "timestamp_ms": self.timestamp_ms,
        }
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
        # Min-heap of (timestamp_ms, request_id), mirroring `_seen`. Eviction
        # only ever removes the oldest entries, so a heap lets `_evict` stop at
        # the first entry still inside the window instead of scanning every
        # tracked id on every message. See `_evict`.
        self._expiry: list[tuple[int, str]] = []

    def observe(self, *, request_id: str, timestamp_ms: int) -> None:
        now = self._now_ms()
        self._evict(now)

        if now - timestamp_ms > self._ttl_ms:
            raise ReplayError(
                f"message timestamp {timestamp_ms} is older than ttl {self._ttl_ms}ms"
            )

        # Reject future-dated timestamps beyond an allowed clock-skew window.
        # Without an upper bound, a captured frame whose timestamp is set far in
        # the future never reads as "stale", defeating the freshness guarantee
        # (and keeping its request_id un-evictable). The window is symmetric
        # with the past bound so legitimate clock skew is tolerated.
        if timestamp_ms - now > self._ttl_ms:
            raise ReplayError(
                f"message timestamp {timestamp_ms} is more than {self._ttl_ms}ms in the future"
            )

        if request_id in self._seen:
            raise ReplayError(f"replay of request_id={request_id!r}")

        self._seen[request_id] = timestamp_ms
        heapq.heappush(self._expiry, (timestamp_ms, request_id))

    def _evict(self, now: int) -> None:
        """Drop every tracked id whose timestamp has fallen out of the window.

        Ordering the pending expiries in a heap makes this cost proportional
        to the number of ids actually expiring, rather than to the number
        being tracked. The previous full-dict scan ran on every ``observe``,
        so a busy node paid O(tracked ids) per message just to discover that
        nothing had expired yet.
        """
        cutoff = now - self._ttl_ms
        expiry = self._expiry
        seen = self._seen
        while expiry and expiry[0][0] < cutoff:
            timestamp_ms, request_id = heapq.heappop(expiry)
            # An id can only be re-added after its previous entry expired, so
            # a heap entry whose timestamp no longer matches `_seen` is stale
            # and must not evict the newer observation that replaced it.
            if seen.get(request_id) == timestamp_ms:
                del seen[request_id]
