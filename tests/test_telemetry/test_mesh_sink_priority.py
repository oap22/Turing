"""Priority events route via TCP-WHISPER instead of UDP SHOUT (#46)."""

from __future__ import annotations

from turing.mesh.protocol import MeshMessage, MessageType
from turing.telemetry.bus import TelemetryEvent
from turing.telemetry.mesh_sink import MeshTelemetrySink


class _CapturingPublisher:
    def __init__(self) -> None:
        self.shouts: list[tuple[str, MeshMessage]] = []
        self.whispers: list[tuple[str, MeshMessage]] = []

    def shout(self, group: str, message: MeshMessage) -> None:
        self.shouts.append((group, message))

    def whisper(self, peer: str, message: MeshMessage) -> None:
        self.whispers.append((peer, message))


def _evt(*, priority: str | None = None, error: str | None = None) -> TelemetryEvent:
    payload: dict[str, object] = {}
    if priority is not None:
        payload["priority"] = priority
    return TelemetryEvent(
        name="safety.check.end",
        stream="safety.check",
        seq=1,
        timestamp_ms=1000,
        duration_ms=5,
        error=error,
        payload=payload,
    )


class TestPriorityRouting:
    def test_priority_high_event_uses_whisper_to_pi_alpha(self) -> None:
        pub = _CapturingPublisher()
        sink = MeshTelemetrySink(
            pub,
            group="telemetry",
            node_id="n1",
            node_name="pi-beta",
            priority_peer="pi-alpha",
        )
        sink(_evt(priority="high"))
        assert len(pub.whispers) == 1
        peer, msg = pub.whispers[0]
        assert peer == "pi-alpha"
        assert msg.type == MessageType.TELEMETRY_PRIORITY
        # Priority events do NOT also shout — that would defeat the point
        # of the dedicated channel.
        assert pub.shouts == []

    def test_normal_event_keeps_shouting(self) -> None:
        pub = _CapturingPublisher()
        sink = MeshTelemetrySink(
            pub,
            group="telemetry",
            node_id="n1",
            node_name="pi-beta",
            priority_peer="pi-alpha",
        )
        sink(_evt())
        assert pub.shouts and not pub.whispers

    def test_priority_falls_back_to_shout_when_no_priority_peer(self) -> None:
        """If the operator hasn't configured a WHISPER target, slice 2's
        SHOUT path is the safe default — a priority event is no worse off
        than before the slice 8 wiring landed."""
        pub = _CapturingPublisher()
        sink = MeshTelemetrySink(
            pub,
            group="telemetry",
            node_id="n1",
            node_name="pi-beta",
            priority_peer=None,
        )
        sink(_evt(priority="high"))
        assert pub.shouts and not pub.whispers
