"""In-process telemetry event bus.

A lightweight, non-blocking event sink. Producers call :meth:`Telemetry.emit`
which fans out to registered sinks; sink failures are swallowed so a misbehaving
downstream cannot break the caller's hot path. Until the mesh-transport sink
lands in slice 2, the only sink is a structlog debug logger.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

EventSink = Callable[["TelemetryEvent"], None]


@dataclass(frozen=True)
class TelemetryEvent:
    """A single observation flowing through the bus.

    ``stream`` groups related events (start/end/error) so consumers can pair
    them by ``seq``. ``seq`` is monotonic per stream and assigned by the bus.
    """

    name: str
    stream: str
    seq: int
    timestamp_ms: int
    duration_ms: float | None = None
    error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class Telemetry:
    """Non-blocking event bus with per-stream monotonic sequence numbers."""

    def __init__(self) -> None:
        self._seq: dict[str, int] = {}
        self._seq_lock = threading.Lock()
        self._sinks: list[EventSink] = [_structlog_sink]

    def next_seq(self, stream: str) -> int:
        with self._seq_lock:
            n = self._seq.get(stream, 0) + 1
            self._seq[stream] = n
            return n

    def emit(self, event: TelemetryEvent) -> None:
        for sink in list(self._sinks):
            try:
                sink(event)
            except Exception:
                logger.warning("telemetry_sink_failed", sink=getattr(sink, "__name__", repr(sink)))

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink: EventSink) -> None:
        with contextlib.suppress(ValueError):
            self._sinks.remove(sink)


def _structlog_sink(event: TelemetryEvent) -> None:
    logger.debug(
        "telemetry_event",
        name=event.name,
        stream=event.stream,
        seq=event.seq,
        duration_ms=event.duration_ms,
        error=event.error,
        **event.payload,
    )


_SINGLETON = Telemetry()


def get_telemetry() -> Telemetry:
    """Return the process-wide :class:`Telemetry` instance."""
    import sys

    return sys.modules[__name__]._SINGLETON  # type: ignore[no-any-return]


def now_ms() -> int:
    return int(time.time() * 1000)
