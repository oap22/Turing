"""Per-(node, stream) sequence-gap detector.

Producers tag every event with a per-stream monotonic ``seq``. The receiver
calls :meth:`GapDetector.observe` for each arrival; the detector returns:

- ``None`` when the seq slots in cleanly (or buffers within the reorder window)
- ``Reset`` when a stream's seq drops to ``1`` after a higher value (indicates
  the producer restarted; not a real gap)
- ``GapDetected`` when a seq jumps ahead past the reorder window, naming the
  inclusive ``(first_missing, last_missing)`` range

A small reorder window absorbs the common UDP case where ``seq=3`` arrives a
few milliseconds before ``seq=2``; truly missing seqs eventually fall out of
the window and are reported.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GapDetected:
    node: str
    stream: str
    missing: tuple[int, int]  # inclusive (first, last)


@dataclass(frozen=True)
class Reset:
    node: str
    stream: str


class GapDetector:
    def __init__(self, *, reorder_window: int = 4) -> None:
        self._window = max(0, reorder_window)
        # Per-(node, stream) state: highest contiguously-acknowledged seq.
        self._last: dict[tuple[str, str], int] = {}
        # Per-(node, stream) reorder buffer of seqs awaiting their predecessors.
        self._pending: dict[tuple[str, str], set[int]] = {}

    def observe(self, *, node: str, stream: str, seq: int) -> object | None:
        key = (node, stream)
        last = self._last.get(key)

        # First sighting of this stream — establish the baseline.
        if last is None:
            self._last[key] = seq
            return None

        # Stream reset: producer rebooted, seq is back to 1 from something higher.
        if seq == 1 and last > 1:
            self._last[key] = 1
            self._pending.pop(key, None)
            return Reset(node=node, stream=stream)

        # In-order or duplicate within window — no gap.
        if seq == last + 1:
            self._last[key] = seq
            self._drain_pending(key)
            return None
        if seq <= last:
            return None

        # Future seq: stash it. If it's still inside the reorder window, no
        # gap yet; if it pushes past the window, we have to flush.
        pending = self._pending.setdefault(key, set())
        pending.add(seq)
        if seq - last - 1 <= self._window:
            return None

        first_missing = last + 1
        last_missing = seq - 1
        # Treat everything up to seq as acknowledged so we don't keep firing.
        self._last[key] = seq
        # Drop pending entries that are now in the past; keep ones strictly
        # newer than the reported range.
        pending = {p for p in pending if p > seq}
        if pending:
            self._pending[key] = pending
        else:
            self._pending.pop(key, None)
        return GapDetected(node=node, stream=stream, missing=(first_missing, last_missing))

    def _drain_pending(self, key: tuple[str, str]) -> None:
        pending = self._pending.get(key)
        if not pending:
            return
        last = self._last[key]
        while last + 1 in pending:
            last += 1
            pending.discard(last)
        self._last[key] = last
        if not pending:
            self._pending.pop(key, None)
