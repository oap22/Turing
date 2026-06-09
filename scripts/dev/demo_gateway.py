"""Boot the real gateway wiring with seeded data for a local preview.

This is the script the README's "Preview locally with seeded data" section
documents: it assembles the same object graph the standalone
``turing-gateway`` entrypoint builds (rewards store, queue/chat managers,
in-memory ring buffer + telemetry sink, uvicorn via ``GatewayService``) and
then seeds every operator pane so the webui **and** TUI can be driven
without a live fleet:

- queue items across all five columns (proposed → curated),
- a chat thread with completed subtasks (thumbable) and one mid-stream,
- a Surface + 4-Jetson specs panel (one hot/danger temp, one near-full
  disk, one stale peer),
- a trace backlog plus a live telemetry pulse every few seconds,
- a firing temperature alert, re-broadcast so late WS joiners see it.

Unlike ``turing-gateway`` this never calls ``loop.add_signal_handler``
(unsupported on Windows' Proactor loop) — Ctrl+C lands as
``KeyboardInterrupt`` through ``asyncio.run``.

Usage::

    TURING_GATEWAY_TOKEN=demo python scripts/dev/demo_gateway.py --port 8765
    # webui: http://127.0.0.1:8765/token-handoff?token=demo
    # TUI:   TURING_GATEWAY_URL=http://127.0.0.1:8765 TURING_GATEWAY_TOKEN=demo \
    #        ./tui/target/release/turing-tui

State is in-memory and resets on restart.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import time
from dataclasses import replace
from pathlib import Path

import uvicorn

from turing.coordinator.alerts.types import TEMP_DANGER, Alert
from turing.coordinator.episode_rewards import EpisodeRewardsStore
from turing.gateway.app import create_app
from turing.gateway.auth import GatewayAuth
from turing.gateway.chat_manager import ChatManager
from turing.gateway.queue_manager import QueueItem, QueueManager
from turing.gateway.ring_buffer import RingBuffer, RingBufferConfig
from turing.gateway.spa import spa_assets_path
from turing.gateway.telemetry_sink import TelemetrySink
from turing.mesh.node import PeerInfo
from turing.mesh.protocol import MeshMessage, MessageType
from turing.specs.collector import NodeSpecs

GIB = 1024**3

PULSE_INTERVAL_S = 5.0
ALERT_REBROADCAST_S = 20.0


def _now_ms() -> int:
    return int(time.time() * 1000)


class DemoMeshNode:
    """Duck-typed stand-in for ``MeshNode`` — just what ``/peers`` reads."""

    def __init__(self, *, node_id: str, capabilities: list[str], self_specs: NodeSpecs,
                 peers: dict[str, PeerInfo]) -> None:
        self.node_id = node_id
        self.capabilities = capabilities
        self.self_specs = self_specs
        self.peers = peers


def _specs(*, model: str, cores: int, ram_gib: float, disk_gib: float, cpu: float,
           mem_used_gib: float, disk_used_gib: float, temp: float | None,
           uptime_s: int, load: float) -> NodeSpecs:
    return NodeSpecs(
        model_name=model,
        os="Linux 6.1 (JetPack)" if "Jetson" in model else "Windows 11 (WSL2)",
        arch="aarch64" if "Jetson" in model else "x86_64",
        cpu_cores=cores,
        ram_total_bytes=int(ram_gib * GIB),
        disk_total_bytes=int(disk_gib * GIB),
        cpu_percent=cpu,
        mem_used_bytes=int(mem_used_gib * GIB),
        disk_used_bytes=int(disk_used_gib * GIB),
        temp_celsius=temp,
        uptime_seconds=uptime_s,
        loadavg_1m=load,
        loadavg_5m=round(load * 0.9, 2),
        loadavg_15m=round(load * 0.8, 2),
    )


def build_demo_mesh() -> DemoMeshNode:
    """A Surface coordinator + 4 Jetson workers (one hot, one full, one stale)."""
    now = time.time()
    caps = ["shell", "web_search", "python", "summarize"]
    peers = {
        "jetson-1": PeerInfo(
            node_id="jetson-1", name="jetson-1", capabilities=caps,
            last_seen=now, address="100.64.0.11",
            specs=_specs(model="Jetson Orin Nano", cores=6, ram_gib=8, disk_gib=512,
                         cpu=34.0, mem_used_gib=3.1, disk_used_gib=210.0, temp=55.0,
                         uptime_s=4 * 86400, load=1.2),
        ),
        "jetson-2": PeerInfo(
            node_id="jetson-2", name="jetson-2", capabilities=caps,
            last_seen=now, address="100.64.0.12",
            specs=_specs(model="Jetson Orin Nano", cores=6, ram_gib=8, disk_gib=512,
                         cpu=91.0, mem_used_gib=6.7, disk_used_gib=233.0, temp=84.2,
                         uptime_s=11 * 86400, load=5.6),
        ),
        "jetson-3": PeerInfo(
            node_id="jetson-3", name="jetson-3", capabilities=caps,
            last_seen=now, address="100.64.0.13",
            specs=_specs(model="Jetson Orin Nano", cores=6, ram_gib=8, disk_gib=512,
                         cpu=12.0, mem_used_gib=2.2, disk_used_gib=492.0, temp=49.0,
                         uptime_s=2 * 86400, load=0.4),
        ),
        # Stale: last heartbeat 5 minutes ago — the UI dims this row.
        "jetson-4": PeerInfo(
            node_id="jetson-4", name="jetson-4", capabilities=caps,
            last_seen=now - 300.0, address="100.64.0.14",
            specs=_specs(model="Jetson Orin Nano", cores=6, ram_gib=8, disk_gib=512,
                         cpu=3.0, mem_used_gib=1.0, disk_used_gib=120.0, temp=41.0,
                         uptime_s=86400, load=0.1),
        ),
    }
    self_specs = _specs(model="Surface coordinator", cores=14, ram_gib=32, disk_gib=1024,
                        cpu=23.5, mem_used_gib=11.4, disk_used_gib=534.0, temp=48.0,
                        uptime_s=3 * 86400, load=0.8)
    return DemoMeshNode(node_id="surface", capabilities=caps,
                        self_specs=self_specs, peers=peers)


async def seed_queue(queue: QueueManager) -> None:
    """One item per column: 2× proposed, approved, in-flight, drafted, 2× curated."""
    base = _now_ms() - 50 * 60_000

    def item(n: int, prompt: str, *, specialty: str = "research",
             proposed_by: str = "operator") -> QueueItem:
        return QueueItem(id=f"q-{n}", prompt=prompt, specialty=specialty,
                         proposed_by=proposed_by, created_at_ms=base + n * 5 * 60_000)

    await queue.add(item(1, "Quantify NVMe thermal-throttle margin during sustained "
                            "adapter training on the Orin Nanos."))
    await queue.add(item(2, "Does the summarize eval regress when the vault reindex "
                            "runs concurrently?", proposed_by="jetson-1"))

    await queue.add(item(3, "Survey speculative-decoding speedups reported for "
                            "sub-3B local models."))
    await queue.approve("q-3")

    await queue.add(item(4, "Replicate the LoRA rank-8 vs rank-16 comparison on the "
                            "research-summarize cases."))
    await queue.approve("q-4")
    await queue.dispatch("q-4")

    await queue.add(item(5, "Estimate nightly token spend if the critic pass moves "
                            "to the cloud model."))
    await queue.approve("q-5")
    await queue.dispatch("q-5")
    await queue.draft("q-5", episode_id="ep-q5")

    await queue.add(item(6, "Which arXiv categories yield the highest curation "
                            "accept-rate this month?"))
    await queue.approve("q-6")
    await queue.dispatch("q-6")
    await queue.draft("q-6", episode_id="ep-q6")
    await queue.accept("q-6")

    await queue.add(item(7, "Summarize the failure modes seen in the 2026-06-05 "
                            "overnight run.", specialty="critic"))
    await queue.approve("q-7")
    await queue.dispatch("q-7")
    await queue.draft("q-7", episode_id="ep-q7")
    await queue.edit("q-7", corrected_answer="Three failures, all NATS reconnect "
                                             "storms — not model regressions.")


async def seed_chat(chat: ChatManager) -> None:
    """One thread: two completed (thumbable) subtasks, one still streaming."""
    sid = "chat-demo-1"
    await chat.submit(session_id=sid,
                      prompt="Compare LoRA rank 8 vs 16 on the summarize eval and "
                             "recommend a default.", specialty="research")

    await chat.plan_subtask(sid, subtask_id="st-1", specialty="research",
                            prompt="Pull rank-8 eval scores", episode_id="ep-chat-1")
    await chat.stream(sid, "st-1",
                      chunk="rank-8: citation_correctness 0.86, claim_preservation "
                            "0.91, voice_match 0.78 (n=120).", done=True)

    await chat.plan_subtask(sid, subtask_id="st-2", specialty="research",
                            prompt="Pull rank-16 eval scores", episode_id="ep-chat-2")
    await chat.stream(sid, "st-2",
                      chunk="rank-16: citation_correctness 0.88, claim_preservation "
                            "0.90, voice_match 0.84 (n=120).", done=True)

    await chat.plan_subtask(sid, subtask_id="st-3", specialty="synthesis",
                            prompt="Synthesize a recommendation",
                            episode_id="ep-chat-3",
                            consumed_upstreams=["ep-chat-1", "ep-chat-2"])
    await chat.stream(sid, "st-3",
                      chunk="rank-16 wins voice_match by 6 points at equal claim "
                            "preservation; drafting cost analysis", done=False)


async def seed_trace(buffer: RingBuffer) -> None:
    """A backlog of redacted telemetry events over the last ~10 minutes."""
    events = [
        ("jetson-1", "llm.complete", 412.0, None, {"model": "gemma3:1b",
         "prompt_sample": "Summarize arXiv:2605.01234 abstract…"}),
        ("jetson-2", "tool.exec", 1450.0, None, {"tool": "web_search",
         "prompt_sample": "speculative decoding orin nano"}),
        ("surface", "mesh.dispatch", 12.0, None, {"subtask": "q-4/step-1"}),
        ("jetson-1", "memory.retrieve", 38.0, None, {"k": 8}),
        ("jetson-3", "llm.complete", 980.0, None, {"model": "gemma3:1b",
         "prompt_sample": "Extract claims from the draft…"}),
        ("jetson-2", "tool.exec", 30050.0,
         "TimeoutError: shell tool exceeded 30s", {"tool": "shell"}),
        ("surface", "llm.complete", 2210.0, None, {"model": "claude-sonnet",
         "prompt_sample": "Plan a DAG for: Compare LoRA rank 8 vs 16…"}),
        ("jetson-1", "embed.encode", 64.0, None, {"texts": 16}),
        ("surface", "mesh.dispatch", 9.0, None, {"subtask": "q-5/step-2"}),
        ("jetson-3", "tool.exec", 310.0, None, {"tool": "python"}),
    ]
    start = _now_ms() - 10 * 60_000
    seqs: dict[str, int] = {}
    for i, (node, event_type, duration_ms, error, payload) in enumerate(events * 3):
        seqs[node] = seqs.get(node, 0) + 1
        await buffer.append({
            "node_name": node,
            "event_type": event_type,
            "seq": seqs[node],
            "timestamp_ms": start + i * 20_000,
            "duration_ms": duration_ms,
            "error": error,
            "payload": payload,
        })


async def pulse(sink: TelemetrySink, mesh: DemoMeshNode) -> None:
    """Live telemetry: a trace/metric frame every few seconds + fresh specs."""
    nodes = ["surface", "jetson-1", "jetson-2", "jetson-3"]
    names = ["llm.complete.end", "tool.exec.end", "memory.retrieve.end",
             "mesh.dispatch.end"]
    seqs: dict[str, int] = {}
    n = 0
    while True:
        await asyncio.sleep(PULSE_INTERVAL_S)
        n += 1
        node = nodes[n % len(nodes)]
        seqs[node] = seqs.get(node, 0) + 1
        message = MeshMessage(
            type=MessageType.TELEMETRY,
            sender_id=node,
            sender_name=node,
            payload={
                "stream": "demo",
                "event_name": names[n % len(names)],
                "seq": seqs[node],
                "timestamp_ms": _now_ms(),
                "duration_ms": 40 + (n * 37) % 900,
                "error": None,
                "payload": {"prompt_sample": f"demo pulse #{n}"},
            },
        )
        await sink.on_mesh_message(message)

        # Keep live peers fresh (jetson-4 stays stale on purpose) and jiggle
        # the live readings so the specs pane visibly updates.
        for peer in mesh.peers.values():
            if peer.node_id == "jetson-4" or peer.specs is None:
                continue
            peer.touch()
            drift = ((n + hash(peer.node_id)) % 7) - 3
            peer.specs = replace(
                peer.specs,
                cpu_percent=max(1.0, min(99.0, peer.specs.cpu_percent + drift)),
                uptime_seconds=peer.specs.uptime_seconds + int(PULSE_INTERVAL_S),
            )


async def alert_loop(sink: TelemetrySink) -> None:
    """Re-broadcast the firing temp alert so late WS joiners see the banner."""
    while True:
        alert = Alert(
            node_id="jetson-2",
            node_name="jetson-2",
            field="temp_celsius",
            severity="danger",
            value=84.2,
            threshold=TEMP_DANGER,
            state="alerting",
            fired_at_ms=_now_ms(),
        )
        await sink._broadcast(alert.to_frame())  # noqa: SLF001 — same hookup the dispatcher uses
        await asyncio.sleep(ALERT_REBROADCAST_S)


async def run(args: argparse.Namespace) -> None:
    token = os.environ.get("TURING_GATEWAY_TOKEN") or "demo"

    ring_buffer = RingBuffer(RingBufferConfig(
        path=Path(":memory:"), retention_seconds=3600, max_bytes=16 * 1024 * 1024))
    await ring_buffer.open()
    sink = TelemetrySink(buffer=ring_buffer)
    rewards = EpisodeRewardsStore()
    queue = QueueManager(rewards=rewards, broadcast=sink._broadcast, now_ms=_now_ms)
    chat = ChatManager(rewards=rewards, broadcast=sink._broadcast, now_ms=_now_ms)
    mesh = build_demo_mesh()

    await seed_queue(queue)
    await seed_chat(chat)
    await seed_trace(ring_buffer)

    # create_app directly (the same wiring as ``turing.gateway.__main__.build_app``)
    # rather than GatewayService: the service wrapper never forwards a ring
    # buffer, which would leave the ``/api/events`` trace backlog empty.
    app = create_app(
        auth=GatewayAuth(token=token),
        node_name="surface",
        spa_assets_dir=spa_assets_path(),
        ring_buffer=ring_buffer,
        mesh_node=mesh,  # type: ignore[arg-type] — duck-typed, /peers-shaped
        telemetry_sink=sink,
        alert_dispatcher=None,
        queue_manager=queue,
        chat_manager=chat,
    )
    server = uvicorn.Server(uvicorn.Config(
        app, host=args.bind, port=args.port, log_level="info", lifespan="off"))
    server_task = asyncio.create_task(server.serve())

    print()
    print("demo gateway up — state is in-memory and resets on restart")
    print(f"  webui: http://{args.bind}:{args.port}/token-handoff?token={token}")
    print(f"  tui:   TURING_GATEWAY_URL=http://{args.bind}:{args.port} "
          f"TURING_GATEWAY_TOKEN={token} turing-tui")
    print()

    tasks = [asyncio.create_task(pulse(sink, mesh)),
             asyncio.create_task(alert_loop(sink))]
    try:
        await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server_task, timeout=5.0)
        with contextlib.suppress(Exception):
            await ring_buffer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
