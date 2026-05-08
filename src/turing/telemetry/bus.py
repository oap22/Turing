"""Telemetry event bus + @traced decorator.

Per PRD #38 US 16, 19, 20: non-blocking emit, monotonic per-stream seq,
one-line decorator that emits start/end/error events around any function.
"""

from __future__ import annotations

import functools
import inspect
import queue
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

Priority = Literal["normal", "high"]
T = TypeVar("T")


@dataclass(frozen=True)
class Event:
    node_id: str
    node_name: str
    event_type: str
    seq: int
    ts: float
    priority: Priority
    payload: dict[str, Any] = field(default_factory=dict)


class Telemetry:
    """Non-blocking event bus.

    `emit_sync` is safe to call from anywhere — including inside the agent
    loop's hot path. It uses a bounded queue and silently drops events when
    the queue is full (counted in `dropped_count`); the drainer task is
    responsible for keeping up.
    """

    def __init__(
        self,
        *,
        node_id: str,
        node_name: str,
        max_queue_size: int = 10_000,
    ) -> None:
        self.node_id = node_id
        self.node_name = node_name
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=max_queue_size)
        self._seq: defaultdict[str, int] = defaultdict(int)
        self.dropped_count = 0

    def emit_sync(
        self,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        priority: Priority = "normal",
    ) -> None:
        self._seq[event_type] += 1
        ev = Event(
            node_id=self.node_id,
            node_name=self.node_name,
            event_type=event_type,
            seq=self._seq[event_type],
            ts=time.time(),
            priority=priority,
            payload=payload or {},
        )
        try:
            self._queue.put_nowait(ev)
        except queue.Full:
            self.dropped_count += 1

    def drain_for_test(self) -> list[Event]:
        """Drain the queue. Test-only — production code uses an async drainer."""
        out: list[Event] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out


def traced(event_name: str, *, bus: Telemetry) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Wrap a sync or async callable to emit `<event_name>.start/.end/.error`.

    The wrapper is non-blocking; on exception the original error propagates
    after an `.error` event is emitted.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                bus.emit_sync(f"{event_name}.start")
                try:
                    result = await func(*args, **kwargs)
                except BaseException as exc:
                    bus.emit_sync(
                        f"{event_name}.error",
                        payload={
                            "error_type": type(exc).__name__,
                            "duration_ms": int((time.perf_counter() - start) * 1000),
                        },
                        priority="high",
                    )
                    raise
                bus.emit_sync(
                    f"{event_name}.end",
                    payload={"duration_ms": int((time.perf_counter() - start) * 1000)},
                )
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            bus.emit_sync(f"{event_name}.start")
            try:
                result = func(*args, **kwargs)
            except BaseException as exc:
                bus.emit_sync(
                    f"{event_name}.error",
                    payload={
                        "error_type": type(exc).__name__,
                        "duration_ms": int((time.perf_counter() - start) * 1000),
                    },
                    priority="high",
                )
                raise
            bus.emit_sync(
                f"{event_name}.end",
                payload={"duration_ms": int((time.perf_counter() - start) * 1000)},
            )
            return result

        return sync_wrapper

    return decorator
