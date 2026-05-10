"""Tests for the mesh-transport telemetry sink and subscriber."""

from __future__ import annotations

from turing.mesh.protocol import MeshMessage, MessageType
from turing.telemetry.bus import Telemetry, TelemetryEvent
from turing.telemetry.mesh_sink import MeshTelemetrySink, TelemetrySubscriber


class _CapturingPublisher:
    """Stand-in for the Pyre/Zyre node: records (group, message) tuples."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, MeshMessage]] = []

    def shout(self, group: str, message: MeshMessage) -> None:
        self.sent.append((group, message))


def _evt(name: str = "x.start", **overrides) -> TelemetryEvent:  # type: ignore[no-untyped-def]
    base = {
        "name": name,
        "stream": "x",
        "seq": 1,
        "timestamp_ms": 0,
        "duration_ms": None,
        "error": None,
        "payload": {},
    }
    base.update(overrides)
    return TelemetryEvent(**base)


class TestMeshTelemetrySink:
    def test_shouts_event_to_telemetry_group(self) -> None:
        pub = _CapturingPublisher()
        sink = MeshTelemetrySink(pub, group="telemetry", node_id="node-1", node_name="pi-alpha")

        sink(_evt())

        assert len(pub.sent) == 1
        group, msg = pub.sent[0]
        assert group == "telemetry"
        assert msg.type == MessageType.TELEMETRY

    def test_high_priority_payload_uses_priority_message_type(self) -> None:
        pub = _CapturingPublisher()
        sink = MeshTelemetrySink(pub, group="telemetry", node_id="n", node_name="pi-alpha")

        sink(_evt(name="safety.check.end", payload={"priority": "high", "rule": "rm-rf"}))

        _, msg = pub.sent[-1]
        assert msg.type == MessageType.TELEMETRY_PRIORITY

    def test_message_carries_event_payload(self) -> None:
        pub = _CapturingPublisher()
        sink = MeshTelemetrySink(pub, group="telemetry", node_id="n", node_name="pi-alpha")

        sink(_evt(name="llm.complete.end", duration_ms=12.5, payload={"provider": "cloud"}))

        _, msg = pub.sent[-1]
        assert msg.payload["event_name"] == "llm.complete.end"
        assert msg.payload["duration_ms"] == 12.5
        assert msg.payload["payload"]["provider"] == "cloud"

    def test_publisher_failure_swallowed(self) -> None:
        class ExplodingPublisher:
            def shout(self, group: str, message: MeshMessage) -> None:
                raise RuntimeError("network boom")

        sink = MeshTelemetrySink(
            ExplodingPublisher(), group="telemetry", node_id="n", node_name="pi-alpha"
        )

        # Must not raise — telemetry can never break the producer.
        sink(_evt())


class TestTelemetrySubscriber:
    def test_received_messages_are_handled(self) -> None:
        """Hook into the subscriber's internal handler to verify dispatch.

        We don't assert structlog log emission directly because structlog
        bypasses ``caplog`` by default; intercepting ``_handle`` is cleaner
        and tests the behaviour we actually care about (the subscriber routes
        TELEMETRY messages to its handler) without coupling to the log sink.
        """
        subscriber = TelemetrySubscriber()
        seen: list[MeshMessage] = []
        subscriber._handle = seen.append  # type: ignore[assignment]

        msg = MeshMessage(
            type=MessageType.TELEMETRY,
            sender_id="other",
            sender_name="pi-beta",
            payload={
                "event_name": "tool.dispatch.end",
                "stream": "tool.dispatch",
                "seq": 7,
                "duration_ms": 42.0,
                "payload": {},
            },
        )
        subscriber.on_message(msg)
        assert len(seen) == 1
        assert seen[0].payload["event_name"] == "tool.dispatch.end"

    def test_ignores_non_telemetry_messages(self) -> None:
        subscriber = TelemetrySubscriber()
        # Should be safe to call with a non-telemetry message; it just no-ops.
        subscriber.on_message(
            MeshMessage(
                type=MessageType.HEARTBEAT,
                sender_id="x",
                sender_name="pi-x",
                payload={},
            )
        )


class TestTwoNodeFanout:
    """Sink on node A → Subscriber on node B receives the event."""

    def test_event_emitted_on_a_seen_on_b(self) -> None:
        # A "bus" is a list shared by both ends; a real Zyre group does the same.
        wire: list[MeshMessage] = []

        class Pub:
            def shout(self, group: str, message: MeshMessage) -> None:
                wire.append(message)

        sink = MeshTelemetrySink(Pub(), group="telemetry", node_id="a", node_name="pi-a")
        subscriber = TelemetrySubscriber()
        seen_streams: list[str] = []

        original_handle = subscriber._handle

        def capturing_handle(msg: MeshMessage) -> None:
            seen_streams.append(msg.payload.get("stream", ""))
            original_handle(msg)

        subscriber._handle = capturing_handle  # type: ignore[assignment]

        sink(_evt(name="tool.dispatch.end", stream="tool.dispatch", seq=3))
        for msg in wire:
            subscriber.on_message(msg)

        assert "tool.dispatch" in seen_streams


class TestMessageTypes:
    def test_telemetry_message_types_exist(self) -> None:
        assert MessageType.TELEMETRY.value == "telemetry"
        assert MessageType.TELEMETRY_PRIORITY.value == "telemetry_priority"

    def test_mesh_message_round_trip_with_telemetry_type(self) -> None:
        msg = MeshMessage(
            type=MessageType.TELEMETRY,
            sender_id="n",
            sender_name="pi-a",
            payload={"event_name": "x.end"},
        )
        round_tripped = MeshMessage.from_json(msg.to_json())
        assert round_tripped.type == MessageType.TELEMETRY


class TestBusIntegration:
    """Wiring the sink into the Telemetry bus drains events to the publisher."""

    def test_emit_routes_through_sink(self) -> None:
        pub = _CapturingPublisher()
        tel = Telemetry()
        sink = MeshTelemetrySink(pub, group="telemetry", node_id="n", node_name="pi-alpha")
        tel.add_sink(sink)

        tel.emit(_evt(name="memory.retrieve.end"))
        assert pub.sent[-1][1].payload["event_name"] == "memory.retrieve.end"
