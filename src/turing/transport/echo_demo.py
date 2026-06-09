"""Terminal-runnable demo: coordinator publishes echo subtask, worker replies.

Exists for the issue-#4 acceptance "verifiable from a terminal demo." Uses
`InMemoryBus` by default so the demo runs without a NATS broker; pass
`--nats-url` on the CLI to swap in `NatsBus`. The signing/verification path is
identical in both cases — the only thing that changes is the byte-transport
underneath.

Run:

    python -m turing.transport.echo_demo
"""

from __future__ import annotations

import argparse
import asyncio
from typing import TYPE_CHECKING

from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from turing.transport.bus import Bus

ECHO_REQUEST = "echo.request"
ECHO_RESULT = "echo.result"


async def run_echo_round_trip(
    *,
    payload: bytes,
    bus_factory: Callable[[], Awaitable[Bus]] | None = None,
) -> bytes:
    """Run a single signed echo round trip and return the result payload."""
    bus = await bus_factory() if bus_factory else InMemoryBus()

    coordinator_signer = MessageSigner.generate()
    worker_signer = MessageSigner.generate()
    trusted = {
        "coordinator": coordinator_signer.public_key,
        "worker": worker_signer.public_key,
    }

    coordinator = SignedTransport(
        bus=bus, signer=coordinator_signer, trusted_keys=trusted, now_ms=lambda: 0
    )
    worker = SignedTransport(bus=bus, signer=worker_signer, trusted_keys=trusted, now_ms=lambda: 0)

    result_received: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()

    async def on_request(msg: MeshMessage) -> None:
        reply = MeshMessage(
            request_id=f"{msg.request_id}.reply",
            sender_id="worker",
            subject=ECHO_RESULT,
            payload=msg.payload,
            timestamp_ms=msg.timestamp_ms + 1,
        )
        await worker.publish(reply)

    def on_result(msg: MeshMessage) -> None:
        if not result_received.done():
            result_received.set_result(msg.payload)

    await worker.subscribe(ECHO_REQUEST, on_request)
    await coordinator.subscribe(ECHO_RESULT, on_result)

    await coordinator.publish(
        MeshMessage(
            request_id="demo-1",
            sender_id="coordinator",
            subject=ECHO_REQUEST,
            payload=payload,
            timestamp_ms=0,
        )
    )

    return await asyncio.wait_for(result_received, timeout=2.0)


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Signed echo round-trip demo")
    parser.add_argument("--payload", default="hello world")
    args = parser.parse_args(argv)

    result = await run_echo_round_trip(payload=args.payload.encode("utf-8"))
    print(f"coordinator received signed reply: {result.decode('utf-8')!r}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
