"""Tests for the synchronous branch of the @traced decorator.

``test_tracing.py`` covers the async wrapper; this file exercises the parallel
sync wrapper (start/end on success, error event on raise, payload hook).
"""

from __future__ import annotations

import pytest

from turing.telemetry.bus import Telemetry, TelemetryEvent
from turing.telemetry.tracing import traced


@pytest.fixture
def tel(monkeypatch: pytest.MonkeyPatch) -> Telemetry:
    fresh = Telemetry()
    monkeypatch.setattr("turing.telemetry.bus._SINGLETON", fresh)
    return fresh


class TestSyncSuccess:
    def test_emits_start_and_end(self, tel: Telemetry) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        @traced("sync.alpha")
        def f() -> str:
            return "ok"

        assert f() == "ok"
        assert [e.name for e in captured] == ["sync.alpha.start", "sync.alpha.end"]
        end = next(e for e in captured if e.name == "sync.alpha.end")
        assert end.duration_ms is not None
        assert end.duration_ms >= 0


class TestSyncError:
    def test_exception_emits_error_event_and_propagates(self, tel: Telemetry) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        @traced("sync.beta")
        def f() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            f()

        names = [e.name for e in captured]
        assert "sync.beta.start" in names
        assert "sync.beta.error" in names
        err = next(e for e in captured if e.name == "sync.beta.error")
        assert err.error == "ValueError"
        assert err.duration_ms is not None


class TestSyncPayloadHook:
    def test_payload_callable_is_invoked_per_event(self, tel: Telemetry) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        def payload(kind, args, kwargs, result, exc):  # type: ignore[no-untyped-def]
            data = {"kind": kind, "n_args": len(args)}
            if kind == "end":
                data["result"] = result
            return data

        @traced("sync.gamma", payload=payload)
        def add(a: int, b: int) -> int:
            return a + b

        assert add(2, 3) == 5
        start = next(e for e in captured if e.name == "sync.gamma.start")
        end = next(e for e in captured if e.name == "sync.gamma.end")
        assert start.payload["n_args"] == 2
        assert end.payload["result"] == 5

    def test_payload_exception_is_swallowed(self, tel: Telemetry) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        def payload(*_a, **_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("payload bug")

        @traced("sync.delta", payload=payload)
        def f() -> str:
            return "ok"

        assert f() == "ok"
        names = [e.name for e in captured]
        assert "sync.delta.start" in names
        assert "sync.delta.end" in names
