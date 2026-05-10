"""Tests for the @traced decorator."""

from __future__ import annotations

import asyncio

import pytest

from turing.telemetry.bus import Telemetry, TelemetryEvent
from turing.telemetry.tracing import traced


@pytest.fixture
def tel(monkeypatch: pytest.MonkeyPatch) -> Telemetry:
    """Fresh Telemetry instance, swapped in as the singleton for the test."""
    fresh = Telemetry()
    monkeypatch.setattr("turing.telemetry.bus._SINGLETON", fresh)
    return fresh


class TestAsyncSuccess:
    @pytest.mark.asyncio
    async def test_emits_start_and_end(self, tel: Telemetry) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        @traced("op.alpha")
        async def f() -> str:
            return "ok"

        result = await f()
        assert result == "ok"

        names = [e.name for e in captured]
        assert names == ["op.alpha.start", "op.alpha.end"]

    @pytest.mark.asyncio
    async def test_end_event_has_positive_duration(self, tel: Telemetry) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        @traced("op.alpha")
        async def f() -> None:
            await asyncio.sleep(0.005)

        await f()
        end = next(e for e in captured if e.name == "op.alpha.end")
        assert end.duration_ms is not None
        assert end.duration_ms > 0

    @pytest.mark.asyncio
    async def test_seq_monotonic_within_stream(self, tel: Telemetry) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        @traced("op.alpha")
        async def f() -> None:
            return None

        await f()
        await f()
        seqs = [e.seq for e in captured]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)


class TestAsyncError:
    @pytest.mark.asyncio
    async def test_exception_emits_error_event_and_propagates(self, tel: Telemetry) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        @traced("op.alpha")
        async def f() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await f()

        names = [e.name for e in captured]
        assert "op.alpha.start" in names
        assert "op.alpha.error" in names
        err = next(e for e in captured if e.name == "op.alpha.error")
        assert err.error == "ValueError"
        assert err.duration_ms is not None
        assert err.duration_ms >= 0


class TestPayloadHook:
    @pytest.mark.asyncio
    async def test_payload_callable_is_invoked(self, tel: Telemetry) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        def payload(kind, args, kwargs, result, exc):  # type: ignore[no-untyped-def]
            data = {"kind": kind, "n_args": len(args)}
            if kind == "end":
                data["result"] = result
            return data

        @traced("op.beta", payload=payload)
        async def add(a: int, b: int) -> int:
            return a + b

        await add(2, 3)

        start = next(e for e in captured if e.name == "op.beta.start")
        end = next(e for e in captured if e.name == "op.beta.end")
        assert start.payload["kind"] == "start"
        assert start.payload["n_args"] == 2
        assert end.payload["kind"] == "end"
        assert end.payload["result"] == 5

    @pytest.mark.asyncio
    async def test_payload_exception_does_not_break_tracing(self, tel: Telemetry) -> None:
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        def payload(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("payload bug")

        @traced("op.gamma", payload=payload)
        async def f() -> str:
            return "ok"

        result = await f()
        assert result == "ok"
        names = [e.name for e in captured]
        assert "op.gamma.start" in names
        assert "op.gamma.end" in names
