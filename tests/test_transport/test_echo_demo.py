"""Tests for the echo round-trip demo.

The demo's whole job is to prove the coordinator → worker → coordinator path
works with signed messages. Test the function directly so the same code path
can run from a terminal (`python -m turing.transport.echo_demo`) with no
divergence between demo and test.
"""

from __future__ import annotations

from turing.transport.echo_demo import run_echo_round_trip


async def test_echo_round_trip_returns_payload_after_round_trip() -> None:
    result = await run_echo_round_trip(payload=b"hello")
    assert result == b"hello"
