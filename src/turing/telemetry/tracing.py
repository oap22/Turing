"""@traced decorator: auto-emits start/end/error telemetry events."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable
from typing import Any

import structlog

from turing.telemetry import bus
from turing.telemetry.bus import TelemetryEvent

logger = structlog.get_logger(__name__)

PayloadHook = Callable[..., dict[str, Any]]
"""Signature: ``(kind, args, kwargs, result, exc) -> dict``.

``kind`` is one of ``"start"``, ``"end"``, ``"error"``. ``result`` is set on
``"end"``, ``exc`` is set on ``"error"``. Hook exceptions are swallowed.
"""


def _safe_payload(
    hook: PayloadHook | None,
    kind: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    result: Any,
    exc: BaseException | None,
) -> dict[str, Any]:
    if hook is None:
        return {}
    try:
        out = hook(kind, args, kwargs, result, exc)
        return out or {}
    except Exception:
        logger.warning("telemetry_payload_hook_failed", kind=kind)
        return {}


def traced(event_name: str, *, payload: PayloadHook | None = None) -> Callable[..., Any]:
    """Wrap a function so it emits telemetry events.

    Parameters
    ----------
    event_name:
        The stream key. Three event names are emitted: ``f"{event_name}.start"``,
        ``f"{event_name}.end"``, and ``f"{event_name}.error"``. ``seq`` is
        monotonic across all three within the stream.
    payload:
        Optional callable producing extra payload fields per event. Called as
        ``payload(kind, args, kwargs, result, exc)`` where ``kind`` is one of
        ``"start"`` / ``"end"`` / ``"error"``. Hook exceptions are swallowed.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                tel = bus.get_telemetry()
                start_payload = _safe_payload(payload, "start", args, kwargs, None, None)
                tel.emit(
                    TelemetryEvent(
                        name=f"{event_name}.start",
                        stream=event_name,
                        seq=tel.next_seq(event_name),
                        timestamp_ms=bus.now_ms(),
                        payload=start_payload,
                    )
                )
                t0 = time.monotonic()
                try:
                    result = await fn(*args, **kwargs)
                except BaseException as exc:
                    duration_ms = (time.monotonic() - t0) * 1000
                    err_payload = _safe_payload(payload, "error", args, kwargs, None, exc)
                    tel.emit(
                        TelemetryEvent(
                            name=f"{event_name}.error",
                            stream=event_name,
                            seq=tel.next_seq(event_name),
                            timestamp_ms=bus.now_ms(),
                            duration_ms=duration_ms,
                            error=type(exc).__name__,
                            payload=err_payload,
                        )
                    )
                    raise
                duration_ms = (time.monotonic() - t0) * 1000
                end_payload = _safe_payload(payload, "end", args, kwargs, result, None)
                tel.emit(
                    TelemetryEvent(
                        name=f"{event_name}.end",
                        stream=event_name,
                        seq=tel.next_seq(event_name),
                        timestamp_ms=bus.now_ms(),
                        duration_ms=duration_ms,
                        payload=end_payload,
                    )
                )
                return result

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            tel = bus.get_telemetry()
            start_payload = _safe_payload(payload, "start", args, kwargs, None, None)
            tel.emit(
                TelemetryEvent(
                    name=f"{event_name}.start",
                    stream=event_name,
                    seq=tel.next_seq(event_name),
                    timestamp_ms=bus.now_ms(),
                    payload=start_payload,
                )
            )
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                duration_ms = (time.monotonic() - t0) * 1000
                err_payload = _safe_payload(payload, "error", args, kwargs, None, exc)
                tel.emit(
                    TelemetryEvent(
                        name=f"{event_name}.error",
                        stream=event_name,
                        seq=tel.next_seq(event_name),
                        timestamp_ms=bus.now_ms(),
                        duration_ms=duration_ms,
                        error=type(exc).__name__,
                        payload=err_payload,
                    )
                )
                raise
            duration_ms = (time.monotonic() - t0) * 1000
            end_payload = _safe_payload(payload, "end", args, kwargs, result, None)
            tel.emit(
                TelemetryEvent(
                    name=f"{event_name}.end",
                    stream=event_name,
                    seq=tel.next_seq(event_name),
                    timestamp_ms=bus.now_ms(),
                    duration_ms=duration_ms,
                    payload=end_payload,
                )
            )
            return result

        return sync_wrapper

    return decorator
