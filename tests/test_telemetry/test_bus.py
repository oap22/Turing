"""Tests for the telemetry event bus."""

from __future__ import annotations

import threading

from turing.telemetry.bus import Telemetry, TelemetryEvent


class TestSeqMonotonicity:
    def test_seq_starts_at_one(self) -> None:
        tel = Telemetry()
        assert tel.next_seq("stream.a") == 1

    def test_seq_increments_per_stream(self) -> None:
        tel = Telemetry()
        seqs = [tel.next_seq("stream.a") for _ in range(5)]
        assert seqs == [1, 2, 3, 4, 5]

    def test_streams_have_independent_seq(self) -> None:
        tel = Telemetry()
        a1 = tel.next_seq("stream.a")
        b1 = tel.next_seq("stream.b")
        a2 = tel.next_seq("stream.a")
        b2 = tel.next_seq("stream.b")
        assert (a1, a2) == (1, 2)
        assert (b1, b2) == (1, 2)

    def test_seq_thread_safe(self) -> None:
        tel = Telemetry()
        results: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(100):
                v = tel.next_seq("hot")
                with lock:
                    results.append(v)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) == list(range(1, 801))


class TestEmit:
    def test_emit_calls_registered_sink(self) -> None:
        tel = Telemetry()
        captured: list[TelemetryEvent] = []
        tel.add_sink(captured.append)

        evt = TelemetryEvent(name="x.start", stream="x", seq=1, timestamp_ms=0)
        tel.emit(evt)
        assert captured == [evt]

    def test_emit_never_blocks_when_sink_raises(self) -> None:
        tel = Telemetry()

        def bad_sink(_: TelemetryEvent) -> None:
            raise RuntimeError("sink exploded")

        tel.add_sink(bad_sink)
        # Should not raise to caller
        tel.emit(TelemetryEvent(name="x", stream="x", seq=1, timestamp_ms=0))

    def test_emit_calls_all_sinks_even_if_one_raises(self) -> None:
        tel = Telemetry()
        captured: list[TelemetryEvent] = []

        def bad_sink(_: TelemetryEvent) -> None:
            raise RuntimeError("nope")

        tel.add_sink(bad_sink)
        tel.add_sink(captured.append)

        evt = TelemetryEvent(name="x", stream="x", seq=1, timestamp_ms=0)
        tel.emit(evt)
        assert captured == [evt]


class TestSingleton:
    def test_get_telemetry_returns_same_instance(self) -> None:
        from turing.telemetry import get_telemetry

        assert get_telemetry() is get_telemetry()
