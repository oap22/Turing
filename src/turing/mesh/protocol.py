"""Mesh network message protocol for inter-node communication."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MessageType(StrEnum):
    """Types of messages that can be exchanged between mesh nodes."""

    TASK_REQUEST = "task_request"
    TASK_RESULT = "task_result"
    STATUS_QUERY = "status_query"
    STATUS_RESPONSE = "status_response"
    HEARTBEAT = "heartbeat"
    TELEMETRY = "telemetry"
    # Reserved for slice 8: TCP-WHISPER fallback for never-drop events.
    # Until then, priority events ride the same SHOUT path as TELEMETRY.
    TELEMETRY_PRIORITY = "telemetry_priority"


@dataclass
class MeshMessage:
    """A structured message for mesh network communication.

    Messages are serialized to/from JSON for transport over the Pyre/Zyre
    network layer.
    """

    type: MessageType
    sender_id: str
    sender_name: str
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_json(self) -> str:
        """Serialize the message to a JSON string."""
        return json.dumps(
            {
                "type": self.type.value,
                "sender_id": self.sender_id,
                "sender_name": self.sender_name,
                "payload": self.payload,
                "request_id": self.request_id,
            }
        )

    @classmethod
    def from_json(cls, data: str) -> MeshMessage:
        """Deserialize a MeshMessage from a JSON string.

        Raises:
            json.JSONDecodeError: If the data is not valid JSON.
            KeyError: If required fields are missing.
            ValueError: If the message type is invalid.
        """
        d = json.loads(data)
        return cls(
            type=MessageType(d["type"]),
            sender_id=d["sender_id"],
            sender_name=d["sender_name"],
            payload=d.get("payload", {}),
            request_id=d.get("request_id", str(uuid.uuid4())),
        )

    def to_bytes(self) -> bytes:
        """Serialize the message to UTF-8 bytes for network transport."""
        return self.to_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> MeshMessage:
        """Deserialize a MeshMessage from raw bytes."""
        return cls.from_json(data.decode("utf-8"))
