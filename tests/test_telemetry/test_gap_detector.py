"""Tests for the per-(node, stream) gap detector (#46)."""

from __future__ import annotations

from turing.telemetry.gap_detector import (
    GapDetected,
    GapDetector,
    Reset,
)


class TestSequential:
    def test_in_order_emits_no_event(self) -> None:
        det = GapDetector(reorder_window=2)
        for seq in (1, 2, 3, 4):
            assert det.observe(node="pi-alpha", stream="x", seq=seq) is None

    def test_first_observation_is_not_a_gap(self) -> None:
        det = GapDetector(reorder_window=2)
        # First seq we ever see for a stream — even if it's not 1 — is the
        # baseline, not a gap.
        assert det.observe(node="pi-alpha", stream="x", seq=42) is None


class TestGapDetection:
    def test_skipped_seq_is_a_gap(self) -> None:
        det = GapDetector(reorder_window=0)  # no reorder tolerance
        assert det.observe(node="pi-alpha", stream="x", seq=1) is None
        assert det.observe(node="pi-alpha", stream="x", seq=2) is None
        result = det.observe(node="pi-alpha", stream="x", seq=4)
        assert isinstance(result, GapDetected)
        assert result.node == "pi-alpha"
        assert result.stream == "x"
        assert result.missing == (3, 3)

    def test_multi_seq_gap_reports_full_range(self) -> None:
        det = GapDetector(reorder_window=0)
        assert det.observe(node="pi-alpha", stream="x", seq=1) is None
        result = det.observe(node="pi-alpha", stream="x", seq=5)
        assert isinstance(result, GapDetected)
        assert result.missing == (2, 4)


class TestReorderWindow:
    def test_within_window_reorder_does_not_gap(self) -> None:
        det = GapDetector(reorder_window=2)
        # Arrival order: 1, 3, 2, 4. With window=2, seq=3 buffers (waiting
        # for 2), seq=2 fills the gap, seq=4 progresses cleanly.
        events = [
            det.observe(node="pi-alpha", stream="x", seq=1),
            det.observe(node="pi-alpha", stream="x", seq=3),
            det.observe(node="pi-alpha", stream="x", seq=2),
            det.observe(node="pi-alpha", stream="x", seq=4),
        ]
        assert all(e is None for e in events), events

    def test_beyond_window_still_gaps(self) -> None:
        det = GapDetector(reorder_window=2)
        det.observe(node="pi-alpha", stream="x", seq=1)
        # seq=10 is way past the reorder window — must report a gap
        result = det.observe(node="pi-alpha", stream="x", seq=10)
        assert isinstance(result, GapDetected)
        assert result.missing == (2, 9)


class TestStreamReset:
    def test_seq_returning_to_1_is_a_reset(self) -> None:
        det = GapDetector(reorder_window=2)
        for seq in (1, 2, 3):
            det.observe(node="pi-alpha", stream="x", seq=seq)
        result = det.observe(node="pi-alpha", stream="x", seq=1)
        assert isinstance(result, Reset)
        assert result.node == "pi-alpha"
        assert result.stream == "x"

    def test_after_reset_subsequent_seqs_are_baseline(self) -> None:
        det = GapDetector(reorder_window=0)
        for seq in (1, 2, 3):
            det.observe(node="pi-alpha", stream="x", seq=seq)
        det.observe(node="pi-alpha", stream="x", seq=1)  # reset
        # Now seq=2 must be in-order, not a gap from "3 → 2"
        assert det.observe(node="pi-alpha", stream="x", seq=2) is None


class TestStreamIsolation:
    def test_streams_track_independently(self) -> None:
        det = GapDetector(reorder_window=0)
        det.observe(node="pi-alpha", stream="x", seq=1)
        det.observe(node="pi-alpha", stream="x", seq=2)
        # A different stream's first sighting must NOT be a gap.
        assert det.observe(node="pi-alpha", stream="y", seq=10) is None

    def test_nodes_track_independently(self) -> None:
        det = GapDetector(reorder_window=0)
        det.observe(node="pi-alpha", stream="x", seq=1)
        det.observe(node="pi-alpha", stream="x", seq=2)
        assert det.observe(node="pi-beta", stream="x", seq=99) is None
