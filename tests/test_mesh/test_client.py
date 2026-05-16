"""Tests for ``turing.mesh.client.MeshClient``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from turing.mesh.client import MeshClient
from turing.mesh.node import MeshNode
from turing.mesh.protocol import MeshMessage, MessageType


def _make_node(node_id: str = "n1", node_name: str = "pi-alpha") -> MeshNode:
    return MeshNode(SimpleNamespace(node_id=node_id, node_name=node_name))


@pytest.mark.asyncio
class TestMeshClient:
    async def test_send_task_enqueues_message_and_returns_request_id(self) -> None:
        client = MeshClient(_make_node())
        rid = await client.send_task("peer-2", "do the thing")

        peer_id, msg = await asyncio.wait_for(client.get_next_outbound(), 0.1)
        assert peer_id == "peer-2"
        assert msg.type == MessageType.TASK_REQUEST
        assert msg.payload == {"task": "do the thing"}
        assert msg.request_id == rid
        assert msg.sender_id == "n1"

    async def test_broadcast_uses_none_peer_id(self) -> None:
        client = MeshClient(_make_node())
        msg = MeshMessage(
            type=MessageType.HEARTBEAT,
            sender_id="n1",
            sender_name="pi-alpha",
        )
        await client.broadcast(msg)
        peer_id, sent = await asyncio.wait_for(client.get_next_outbound(), 0.1)
        assert peer_id is None
        assert sent is msg

    async def test_send_heartbeat_broadcasts_capabilities(self) -> None:
        node = _make_node()
        node.capabilities = ["shell", "search"]
        client = MeshClient(node)
        await client.send_heartbeat()

        peer_id, msg = await asyncio.wait_for(client.get_next_outbound(), 0.1)
        assert peer_id is None
        assert msg.type == MessageType.HEARTBEAT
        assert msg.payload == {"capabilities": ["shell", "search"]}

    async def test_wait_for_response_resolves_when_matching_message_arrives(self) -> None:
        client = MeshClient(_make_node())
        rid = await client.send_task("peer-2", "x")
        # Drain outbox so it doesn't interfere.
        await client.get_next_outbound()

        response = MeshMessage(
            type=MessageType.TASK_RESULT,
            sender_id="peer-2",
            sender_name="pi-beta",
            payload={"ok": True},
            request_id=rid,
        )

        async def deliver_soon() -> None:
            await asyncio.sleep(0.01)
            client.handle_incoming(response)

        deliver_task = asyncio.create_task(deliver_soon())
        result = await client.wait_for_response(rid, timeout=1.0)
        await deliver_task
        assert result is response
        assert rid not in client._pending  # cleaned up

    async def test_wait_for_response_times_out(self) -> None:
        client = MeshClient(_make_node())
        with pytest.raises(asyncio.TimeoutError):
            await client.wait_for_response("never", timeout=0.05)
        # Future cleaned up after timeout.
        assert "never" not in client._pending

    async def test_handle_incoming_with_unmatched_request_id_is_ignored(self) -> None:
        client = MeshClient(_make_node())
        msg = MeshMessage(
            type=MessageType.TELEMETRY,
            sender_id="peer-9",
            sender_name="pi-gamma",
            request_id="orphan",
        )
        # Should not raise; just logs.
        client.handle_incoming(msg)
        assert "orphan" not in client._pending

    async def test_query_status_returns_response_payload(self) -> None:
        client = MeshClient(_make_node())

        async def respond() -> None:
            peer_id, sent = await client.get_next_outbound()
            assert peer_id == "peer-2"
            assert sent.type == MessageType.STATUS_QUERY
            response = MeshMessage(
                type=MessageType.STATUS_RESPONSE,
                sender_id="peer-2",
                sender_name="pi-beta",
                payload={"cpu": 0.5},
                request_id=sent.request_id,
            )
            client.handle_incoming(response)

        responder = asyncio.create_task(respond())
        result = await client.query_status("peer-2")
        await responder
        assert result == {"cpu": 0.5}

    async def test_query_status_timeout_returns_error_dict(self, monkeypatch) -> None:
        client = MeshClient(_make_node())

        async def fast_timeout(
            self: MeshClient, request_id: str, timeout: float = 30.0
        ) -> MeshMessage:
            raise TimeoutError

        monkeypatch.setattr(MeshClient, "wait_for_response", fast_timeout)
        result = await client.query_status("peer-missing")
        assert result["error"]
        assert result["peer_id"] == "peer-missing"
