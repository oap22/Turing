"""CapabilityRegistry — coordinator-side authoritative worker registry.

Workers register a `CapabilityManifest` and refresh on heartbeat. The
scheduler queries `find_workers(specialty, required_tools, exclude_busy)` to
pick a target. Workers without a recent heartbeat are dropped from query
results so a crashed Pi never gets dispatched against.

In-flight tracking lives here too: `note_dispatch` / `note_complete` adjust an
`in_flight` counter the registry can compare against `manifest.max_concurrent`
when `exclude_busy=True`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from turing.coordinator.registry.manifest import CapabilityManifest


def _default_clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class _Entry:
    manifest: CapabilityManifest
    last_seen_ms: int
    in_flight: int = 0
    local: bool = True


class CapabilityRegistry:
    def __init__(
        self,
        *,
        now_ms: Callable[[], int] = _default_clock_ms,
        heartbeat_ttl_ms: int = 30_000,
    ) -> None:
        self._now_ms = now_ms
        self._ttl = heartbeat_ttl_ms
        self._entries: dict[str, _Entry] = {}

    def register(self, manifest: CapabilityManifest, *, local: bool = True) -> None:
        existing = self._entries.get(manifest.worker_id)
        in_flight = existing.in_flight if existing is not None else 0
        self._entries[manifest.worker_id] = _Entry(
            manifest=manifest,
            last_seen_ms=self._now_ms(),
            in_flight=in_flight,
            local=local,
        )

    def is_local(self, worker_id: str) -> bool:
        """Return whether ``worker_id`` runs on the coordinator's own node.

        Unknown workers are treated as remote — the orchestrator should fail
        a dispatch loudly rather than silently fall through to the local
        in-process path.
        """
        entry = self._entries.get(worker_id)
        if entry is None:
            return False
        return entry.local

    def heartbeat(self, worker_id: str) -> None:
        entry = self._entries.get(worker_id)
        if entry is None:
            raise KeyError(f"unknown worker {worker_id!r}")
        entry.last_seen_ms = self._now_ms()

    def note_dispatch(self, worker_id: str) -> None:
        self._entries[worker_id].in_flight += 1

    def note_complete(self, worker_id: str) -> None:
        entry = self._entries[worker_id]
        entry.in_flight = max(0, entry.in_flight - 1)

    def find_workers(
        self,
        *,
        specialty: str,
        required_tools: Iterable[str] = (),
        exclude_busy: bool = False,
    ) -> list[CapabilityManifest]:
        required = frozenset(required_tools)
        cutoff = self._now_ms() - self._ttl

        results: list[CapabilityManifest] = []
        for entry in self._entries.values():
            if entry.last_seen_ms < cutoff:
                continue
            manifest = entry.manifest
            if specialty not in manifest.specialties:
                continue
            if not required.issubset(manifest.tools):
                continue
            if exclude_busy and entry.in_flight >= manifest.max_concurrent:
                continue
            results.append(manifest)
        return results
