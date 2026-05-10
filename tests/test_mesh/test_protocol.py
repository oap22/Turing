"""Tests for the mesh network protocol (MeshMessage serialization)."""

from __future__ import annotations

import json
import uuid

import pytest

from turing.mesh.protocol import MeshMessage, MessageType


class TestMeshMessageSerialization:
    """Test JSON serialization and deserialization of MeshMessage."""

    def test_serialize_task_request(self):
        """Test serializing a TASK_REQUEST message."""
        msg = MeshMessage(
            type=MessageType.TASK_REQUEST,
            sender_id="node-1",
            sender_name="pi-alpha",
            payload={"task": "check cpu usage"},
            request_id="req-001",
        )
        json_str = msg.to_json()
        data = json.loads(json_str)

        assert data["type"] == "task_request"
        assert data["sender_id"] == "node-1"
        assert data["sender_name"] == "pi-alpha"
        assert data["payload"]["task"] == "check cpu usage"
        assert data["request_id"] == "req-001"

    def test_deserialize_task_request(self):
        """Test deserializing a TASK_REQUEST message."""
        raw = json.dumps(
            {
                "type": "task_request",
                "sender_id": "node-1",
                "sender_name": "pi-alpha",
                "payload": {"task": "check disk"},
                "request_id": "req-002",
            }
        )
        msg = MeshMessage.from_json(raw)

        assert msg.type == MessageType.TASK_REQUEST
        assert msg.sender_id == "node-1"
        assert msg.sender_name == "pi-alpha"
        assert msg.payload["task"] == "check disk"
        assert msg.request_id == "req-002"

    def test_roundtrip_serialization(self):
        """Test that serialize -> deserialize preserves all fields."""
        original = MeshMessage(
            type=MessageType.TASK_RESULT,
            sender_id="node-2",
            sender_name="pi-beta",
            payload={"result": "ok", "data": [1, 2, 3]},
            request_id="req-003",
        )
        json_str = original.to_json()
        restored = MeshMessage.from_json(json_str)

        assert restored.type == original.type
        assert restored.sender_id == original.sender_id
        assert restored.sender_name == original.sender_name
        assert restored.payload == original.payload
        assert restored.request_id == original.request_id

    def test_default_request_id_generated(self):
        """Test that a UUID request_id is auto-generated."""
        msg = MeshMessage(
            type=MessageType.HEARTBEAT,
            sender_id="node-1",
            sender_name="pi-alpha",
        )
        # Should be a valid UUID.
        parsed = uuid.UUID(msg.request_id)
        assert parsed.version == 4

    def test_empty_payload_default(self):
        """Test that payload defaults to an empty dict."""
        msg = MeshMessage(
            type=MessageType.HEARTBEAT,
            sender_id="node-1",
            sender_name="pi-alpha",
        )
        assert msg.payload == {}

    def test_deserialize_missing_payload(self):
        """Test deserializing when payload is absent."""
        raw = json.dumps(
            {
                "type": "heartbeat",
                "sender_id": "node-1",
                "sender_name": "pi-alpha",
            }
        )
        msg = MeshMessage.from_json(raw)
        assert msg.payload == {}

    def test_deserialize_missing_request_id(self):
        """Test deserializing when request_id is absent (a new one is generated)."""
        raw = json.dumps(
            {
                "type": "status_query",
                "sender_id": "node-1",
                "sender_name": "pi-alpha",
            }
        )
        msg = MeshMessage.from_json(raw)
        assert msg.request_id is not None
        assert len(msg.request_id) > 0


class TestAllMessageTypes:
    """Test serialization for every MessageType variant."""

    @pytest.mark.parametrize(
        "msg_type",
        [
            MessageType.TASK_REQUEST,
            MessageType.TASK_RESULT,
            MessageType.STATUS_QUERY,
            MessageType.STATUS_RESPONSE,
            MessageType.HEARTBEAT,
        ],
    )
    def test_message_type_roundtrip(self, msg_type: MessageType):
        """Test that each message type can be serialized and deserialized."""
        msg = MeshMessage(
            type=msg_type,
            sender_id="test-node",
            sender_name="test",
            payload={"key": "value"},
        )
        json_str = msg.to_json()
        restored = MeshMessage.from_json(json_str)
        assert restored.type == msg_type

    def test_message_type_values(self):
        """Test that MessageType enum values are correct strings."""
        assert MessageType.TASK_REQUEST.value == "task_request"
        assert MessageType.TASK_RESULT.value == "task_result"
        assert MessageType.STATUS_QUERY.value == "status_query"
        assert MessageType.STATUS_RESPONSE.value == "status_response"
        assert MessageType.HEARTBEAT.value == "heartbeat"


class TestInvalidJSON:
    """Test error handling for invalid input."""

    def test_invalid_json_string(self):
        """Test that invalid JSON raises JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            MeshMessage.from_json("not valid json {{{")

    def test_missing_required_field_type(self):
        """Test that missing 'type' field raises an error."""
        raw = json.dumps(
            {
                "sender_id": "node-1",
                "sender_name": "pi-alpha",
            }
        )
        with pytest.raises(KeyError):
            MeshMessage.from_json(raw)

    def test_missing_required_field_sender_id(self):
        """Test that missing 'sender_id' field raises an error."""
        raw = json.dumps(
            {
                "type": "heartbeat",
                "sender_name": "pi-alpha",
            }
        )
        with pytest.raises(KeyError):
            MeshMessage.from_json(raw)

    def test_missing_required_field_sender_name(self):
        """Test that missing 'sender_name' field raises an error."""
        raw = json.dumps(
            {
                "type": "heartbeat",
                "sender_id": "node-1",
            }
        )
        with pytest.raises(KeyError):
            MeshMessage.from_json(raw)

    def test_invalid_message_type(self):
        """Test that an invalid message type raises ValueError."""
        raw = json.dumps(
            {
                "type": "invalid_type",
                "sender_id": "node-1",
                "sender_name": "pi-alpha",
            }
        )
        with pytest.raises(ValueError):
            MeshMessage.from_json(raw)

    def test_empty_json_string(self):
        """Test that an empty string raises JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            MeshMessage.from_json("")


class TestByteSerialization:
    """Test byte-level serialization methods."""

    def test_to_bytes(self):
        """Test serializing to bytes."""
        msg = MeshMessage(
            type=MessageType.HEARTBEAT,
            sender_id="node-1",
            sender_name="pi-alpha",
        )
        data = msg.to_bytes()
        assert isinstance(data, bytes)
        # Should be valid UTF-8 JSON.
        parsed = json.loads(data.decode("utf-8"))
        assert parsed["type"] == "heartbeat"

    def test_from_bytes(self):
        """Test deserializing from bytes."""
        raw = json.dumps(
            {
                "type": "status_response",
                "sender_id": "node-2",
                "sender_name": "pi-beta",
                "payload": {"status": "online"},
                "request_id": "req-100",
            }
        ).encode("utf-8")
        msg = MeshMessage.from_bytes(raw)
        assert msg.type == MessageType.STATUS_RESPONSE
        assert msg.sender_id == "node-2"
        assert msg.payload["status"] == "online"

    def test_bytes_roundtrip(self):
        """Test that to_bytes -> from_bytes preserves all fields."""
        original = MeshMessage(
            type=MessageType.TASK_RESULT,
            sender_id="node-3",
            sender_name="pi-gamma",
            payload={"output": "done"},
            request_id="req-200",
        )
        data = original.to_bytes()
        restored = MeshMessage.from_bytes(data)
        assert restored.type == original.type
        assert restored.sender_id == original.sender_id
        assert restored.sender_name == original.sender_name
        assert restored.payload == original.payload
        assert restored.request_id == original.request_id
