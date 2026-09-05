#!/usr/bin/env python
"""Micro-benchmarks for Turing's hot paths.

Run before and after a performance change to get a real number instead of a
guess::

    .venv/bin/python scripts/dev/bench_hotpath.py            # all benchmarks
    .venv/bin/python scripts/dev/bench_hotpath.py memory     # one group

Every benchmark seeds its own throwaway SQLite database in a temp directory,
so runs are independent and repeatable. Timings are reported as the *best of
N* repeats to suppress scheduler noise, which is what you want when comparing
two revisions of the same code.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import structlog

# Silence the structlog firehose — debug logging inside the measured code
# would otherwise dominate the timings we are trying to read.
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL),
    cache_logger_on_first_use=True,
)

from turing.memory.retriever import MemoryRetriever  # noqa: E402
from turing.memory.store import MemoryStore  # noqa: E402
from turing.memory.vectors import VectorStore  # noqa: E402

# ── harness ──────────────────────────────────────────────────────────────

Result = tuple[str, float, float, int]
_RESULTS: list[Result] = []


async def _time(
    label: str,
    fn: Callable[[], Awaitable[None]],
    *,
    iterations: int = 50,
    repeats: int = 5,
) -> None:
    """Time *fn* and record best-of-*repeats* mean-per-iteration, in ms."""
    await fn()  # warm up caches / page cache

    samples: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter()
        for _ in range(iterations):
            await fn()
        samples.append((time.perf_counter() - start) / iterations * 1000)

    best = min(samples)
    spread = statistics.pstdev(samples) if len(samples) > 1 else 0.0
    _RESULTS.append((label, best, spread, iterations))
    print(f"  {label:<46} {best:8.3f} ms  (±{spread:.3f}, n={iterations}x{repeats})")


class _FakeEmbeddings:
    """Deterministic stand-in for the ONNX model — keeps the benchmark honest.

    The real embedding model's cost is measured separately; mixing it into the
    retrieval benchmark would drown out the database work we care about here.
    """

    ready = True

    def __init__(self, dimension: int = 384) -> None:
        self._vec = [0.01 * (i % 97) + 0.001 for i in range(dimension)]

    async def embed(self, text: str) -> list[float]:
        return self._vec


async def _seed(db_path: str, *, messages: int = 400) -> tuple[MemoryStore, VectorStore]:
    """Build a store with a realistic amount of history already in it."""
    store = MemoryStore(db_path)
    await store.initialize()
    vectors = VectorStore()
    await vectors.initialize(store.db)

    conv_id = await store.create_conversation("channel-bench")
    for i in range(messages):
        msg_id = await store.add_message(
            conv_id,
            "user" if i % 2 == 0 else "assistant",
            f"benchmark message number {i} about deployment and monitoring",
            user_id="user-bench",
            user_name="bench",
        )
        await vectors.add(msg_id, [0.01 * ((i + j) % 97) + 0.001 for j in range(384)])

    for i in range(40):
        await store.add_fact(f"subject-{i}", "relates-to", f"deployment target {i}")
    for i in range(12):
        await store.set_user_preference("user-bench", f"pref_{i}", f"value_{i}")

    return store, vectors


# ── benchmark groups ─────────────────────────────────────────────────────


async def bench_memory() -> None:
    print("\nmemory — per-message retrieval hot path")
    with tempfile.TemporaryDirectory() as tmp:
        store, vectors = await _seed(f"{tmp}/bench.db")
        retriever = MemoryRetriever(store, vectors, _FakeEmbeddings())  # type: ignore[arg-type]

        await _time(
            "MemoryRetriever.retrieve (all 4 sources)",
            lambda: retriever.retrieve(
                "how do I check the deployment status of the node",
                "channel-bench",
                "user-bench",
                limit=10,
            ),  # type: ignore[arg-type,return-value]
        )
        await _time(
            "  └ semantic search only",
            lambda: retriever._fetch_semantic_matches("how do I check the deployment status", 10),  # type: ignore[arg-type,return-value]
        )
        await _time(
            "  └ recent messages only",
            lambda: retriever._fetch_recent_messages("channel-bench", 10),  # type: ignore[arg-type,return-value]
        )
        await _time(
            "  └ fact search only",
            lambda: retriever._fetch_relevant_facts("how do I check the deployment status", 10),  # type: ignore[arg-type,return-value]
        )
        await store.close()


async def bench_writes() -> None:
    print("\nwrites — per-message persistence")
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(f"{tmp}/bench-writes.db")
        await store.initialize()
        conv_id = await store.create_conversation("channel-writes")

        await _time(
            "add_message",
            lambda: store.add_message(conv_id, "user", "a benchmark message", "u", "n"),  # type: ignore[arg-type,return-value]
            iterations=100,
        )
        await _time(
            "touch_conversation",
            lambda: store.touch_conversation(conv_id),  # type: ignore[arg-type,return-value]
            iterations=100,
        )
        await _time(
            "log_audit",
            lambda: store.log_audit("tool_call", "u", "shell", {"cmd": "ls"}, "ok"),  # type: ignore[arg-type,return-value]
            iterations=100,
        )
        await store.close()


async def bench_tools() -> None:
    print("\ntools — per-tool-call overhead")
    from turing.tools.base import ToolRegistry
    from turing.tools.command_safety import check_denylist, classify_command_risk
    from turing.tools.filesystem import FileSystemTool
    from turing.tools.shell import ShellTool
    from turing.tools.system_info import SystemInfoTool

    registry = ToolRegistry()
    registry.register(ShellTool())
    registry.register(FileSystemTool())
    registry.register(SystemInfoTool())

    async def defs() -> None:
        registry.get_definitions()

    async def denylist() -> None:
        check_denylist("cat /var/log/syslog | grep error | head -50")

    async def classify() -> None:
        classify_command_risk("cat /var/log/syslog | grep error | head -50")

    await _time("ToolRegistry.get_definitions", defs, iterations=2000)
    await _time("check_denylist", denylist, iterations=2000)
    await _time("classify_command_risk", classify, iterations=2000)


async def bench_prompt() -> None:
    print("\nagent — per-message prompt assembly")
    from turing.agent.context import AgentContext
    from turing.agent.core import Agent
    from turing.tools.base import ToolRegistry
    from turing.tools.filesystem import FileSystemTool
    from turing.tools.shell import ShellTool
    from turing.tools.system_info import SystemInfoTool

    registry = ToolRegistry()
    registry.register(ShellTool())
    registry.register(FileSystemTool())
    registry.register(SystemInfoTool())

    agent = Agent.__new__(Agent)  # skip __init__ — we only need prompt assembly
    agent.tool_registry = registry
    agent.plugin_registry = None

    context = AgentContext(
        message="how do I check the deployment status",
        channel_id="channel-bench",
        user_id="user-bench",
        user_name="bench",
        node_name="pi-alpha",
        node_id="node-1",
        user_preferences={f"pref_{i}": f"value_{i}" for i in range(12)},
        relevant_facts=[
            {"subject": f"s{i}", "predicate": "is", "object": f"o{i}"} for i in range(20)
        ],
        history=[
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"}
            for i in range(20)
        ],
    )

    async def build_prompt() -> None:
        agent._build_system_prompt(context)

    async def build_messages() -> None:
        agent._build_messages(context)

    await _time("Agent._build_system_prompt", build_prompt, iterations=2000)
    await _time("Agent._build_messages", build_messages, iterations=2000)


async def bench_transport() -> None:
    print("\ntransport — per-message envelope, signing, replay window")
    import json

    from turing.transport.envelope import MeshMessage, ReplayWindow
    from turing.transport.signer import MessageSigner

    signer = MessageSigner.generate()
    trusted = [signer.public_key]

    msg = MeshMessage(
        request_id="req-0000",
        sender_id="pi-alpha",
        subject="mesh.presence.heartbeat",
        payload=json.dumps({"node": "pi-alpha", "uptime": 12345}).encode(),
        timestamp_ms=1_700_000_000_000,
    )

    async def to_bytes() -> None:
        msg.to_bytes()

    async def roundtrip() -> None:
        MeshMessage.from_bytes(msg.to_bytes())

    raw = msg.to_bytes()
    signed = signer.sign(raw)

    async def sign() -> None:
        signer.sign(raw)

    async def verify() -> None:
        signer.verify(signed, trusted_public_keys=trusted)

    await _time("MeshMessage.to_bytes", to_bytes, iterations=2000)
    await _time("MeshMessage to_bytes -> from_bytes", roundtrip, iterations=2000)
    await _time("MessageSigner.sign", sign, iterations=2000)
    await _time("MessageSigner.verify", verify, iterations=2000)

    # Replay window with a realistic backlog of in-window request ids. The
    # eviction scan is what we care about — it runs on every observe().
    window = ReplayWindow(ttl_ms=60_000, now_ms=lambda: 1_700_000_000_000)
    for i in range(5_000):
        window.observe(request_id=f"seed-{i}", timestamp_ms=1_700_000_000_000)

    counter = 0

    async def observe() -> None:
        nonlocal counter
        counter += 1
        window.observe(request_id=f"bench-{counter}", timestamp_ms=1_700_000_000_000)

    await _time("ReplayWindow.observe (5k backlog)", observe, iterations=2000)


GROUPS: dict[str, Callable[[], Awaitable[None]]] = {
    "memory": bench_memory,
    "writes": bench_writes,
    "tools": bench_tools,
    "prompt": bench_prompt,
    "transport": bench_transport,
}


async def main() -> None:
    wanted = sys.argv[1:] or list(GROUPS)
    unknown = [w for w in wanted if w not in GROUPS]
    if unknown:
        print(f"unknown group(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(GROUPS)}", file=sys.stderr)
        raise SystemExit(2)

    for name in wanted:
        try:
            await GROUPS[name]()
        except Exception as exc:  # a broken group shouldn't sink the whole run
            print(f"  !! {name} failed: {type(exc).__name__}: {exc}")

    print(f"\n{len(_RESULTS)} measurement(s)")


if __name__ == "__main__":
    asyncio.run(main())
