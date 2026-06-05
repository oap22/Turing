"""End-to-end walking skeleton for ADR 0009 §2 Phase 0 (#261).

One approved question, mocked NATS (InMemoryBus) / LLM (scripted) / web_fetch
(stub) and a tmp vault, exercised straight through every layer:

    approve → nightly dispatch (RESEARCH kind) → worker runs GroundedResearcher
    → grounded draft in vault/inbox/<q>/ with sources → episode recorded WITH
    the reasoning trajectory (the #261 wire, previously empty) → morning accept
    → MORNING_CURATION reward + reasoning-bearing SFT candidate.

This is the slice's defining acceptance criterion: the named modules don't just
exist, they compose end-to-end on a single question.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from turing.coordinator.dispatch import (
    SubtaskDispatch,
    SubtaskDispatchClient,
    TaskResult,
)
from turing.coordinator.episode_rewards import EpisodeRewardsStore, RewardSource
from turing.coordinator.flywheel import (
    MorningCuration,
    NightlyDispatcher,
    QuestionQueue,
    ResearchQuestion,
)
from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.registry import CapabilityRegistry
from turing.coordinator.registry.manifest import (
    CURRENT_MANIFEST_VERSION,
    CapabilityManifest,
)
from turing.llm.base import LLMResponse, ToolCall
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner
from turing.vault.inbox_writer import InboxDraftWriter
from turing.worker.grounded_handler import GroundedResearchHandler
from turing.worker.grounding import GroundedResearcher
from turing.worker.tools.web_fetch import WebFetchAllowlist

if TYPE_CHECKING:
    from pathlib import Path

SPECIALTY = "ai-ml-generalist"


def _now_ms_factory(start: int = 1_700_000_000_000):
    counter = {"t": start}

    def now() -> int:
        counter["t"] += 1
        return counter["t"]

    return now


@pytest.fixture()
def transports():
    bus = InMemoryBus()
    coord_signer = MessageSigner.generate()
    worker_signer = MessageSigner.generate()
    now = _now_ms_factory()
    coord = SignedTransport(
        bus=bus, signer=coord_signer, trusted_keys=[worker_signer.public_key], now_ms=now
    )
    worker = SignedTransport(
        bus=bus, signer=worker_signer, trusted_keys=[coord_signer.public_key], now_ms=now
    )
    return bus, coord, worker


def _manifest(worker_id: str) -> CapabilityManifest:
    return CapabilityManifest(
        worker_id=worker_id,
        specialties=(SPECIALTY,),
        base_model="qwen2.5:7b",
        adapters=(),
        tools=("vault_query", "web_fetch"),
        hardware="jetson-orin-nano-super",
        max_concurrent=1,
        eval_score=0.5,
        public_key=b"\x01" * 32,
        schema_version=CURRENT_MANIFEST_VERSION,
    )


def _registry(*worker_ids: str) -> CapabilityRegistry:
    reg = CapabilityRegistry(now_ms=lambda: 0, heartbeat_ttl_ms=10_000)
    for wid in worker_ids:
        reg.register(_manifest(wid))
    return reg


class _ScriptedLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self._i = 0

    async def complete(self, messages, system="", tools=None, max_tokens=4096, temperature=0.7):
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


async def _fake_fetcher(url: str) -> tuple[str, str]:
    return (f"Title for {url}", f"Real source text from {url} explaining QLoRA fine-tuning.")


def _fetch_then_answer() -> list[LLMResponse]:
    return [
        LLMResponse(
            content="I should ground this in a real paper before answering.",
            tool_calls=[
                ToolCall(
                    id="t1", name="web_fetch", arguments={"url": "https://arxiv.org/abs/2305.14314"}
                )
            ],
            model="qwen2.5:7b",
            usage={"total_tokens": 50},
        ),
        LLMResponse(
            content="QLoRA fine-tunes a 4-bit quantised base with low-rank adapters.",
            model="qwen2.5:7b",
            usage={"total_tokens": 40},
        ),
    ]


async def _grounded_worker(transport: SignedTransport, worker_id: str, *, tmp_path: Path) -> None:
    """A worker that runs the real GroundedResearchHandler for RESEARCH subtasks."""
    researcher = GroundedResearcher(
        llm=_ScriptedLLM(_fetch_then_answer()),
        allowlist=WebFetchAllowlist(("arxiv.org",)),
        fetcher=_fake_fetcher,
        writer=InboxDraftWriter(vault_root=tmp_path),
        worker_id=worker_id,
        now_ms=1000,
    )
    handler = GroundedResearchHandler(
        researcher=researcher, worker_id=worker_id, now_ms=_now_ms_factory(5000)
    )

    async def handle(msg: MeshMessage) -> None:
        envelope = SubtaskDispatch.from_dict(json.loads(msg.payload.decode("utf-8")))
        result: TaskResult = await handler.handle(envelope)
        reply = MeshMessage(
            request_id=f"r-{envelope.subtask_id}",
            sender_id=worker_id,
            subject=f"subtasks.{envelope.subtask_id}.result",
            payload=json.dumps(result.to_dict()).encode("utf-8"),
            timestamp_ms=1_700_000_000_500,
        )
        await transport.publish(reply)

    await transport.subscribe(f"subtasks.workers.{worker_id}", handle)


@pytest.mark.asyncio
async def test_one_question_runs_dispatch_to_reward_end_to_end(tmp_path: Path, transports) -> None:
    _, coord, worker = transports
    await _grounded_worker(worker, "jetson-1", tmp_path=tmp_path)

    queue = QuestionQueue()
    queue.add(ResearchQuestion("q-qlora", "Explain QLoRA.", SPECIALTY, created_at_ms=1))
    queue.approve("q-qlora")

    episodes = EpisodeStore()
    client = SubtaskDispatchClient(
        transport=coord, sender_id="coordinator", now_ms=_now_ms_factory()
    )
    dispatcher = NightlyDispatcher(
        queue=queue,
        dispatch_client=client,
        episode_store=episodes,
        registry=_registry("jetson-1"),
        now_ms=_now_ms_factory(),
        deadline_ms=10_000,
    )

    report = await dispatcher.run_nightly(batch_id="night-1")

    # ── dispatch closed successfully ─────────────────────────────────────────
    assert report.succeeded == ("q-qlora",)

    # ── episode is reasoning-bearing (the #261 wire) ─────────────────────────
    ep = episodes.get("q-qlora")
    assert ep.task_id == "night-1"
    assert ep.outcome is SubtaskState.COMPLETED
    assert ep.trajectory, "episode trajectory was empty — reasoning never wired home"
    assert any("ground this" in step for step in ep.trajectory)
    assert "QLoRA" in ep.output_text

    # ── grounded draft landed in the inbox with its sources ──────────────────
    draft_path = tmp_path / "vault" / "inbox" / "q-qlora" / "answer.md"
    assert draft_path.exists()
    draft_text = draft_path.read_text(encoding="utf-8")
    assert "## Sources" in draft_text
    assert "arxiv.org" in draft_text

    # ── morning curation: accept → reward + reasoning-bearing SFT pair ────────
    rewards = EpisodeRewardsStore()
    curator = MorningCuration(vault_root=tmp_path, episode_rewards=rewards)
    draft = curator.list_inbox()[0]
    curated, candidate = curator.accept(
        draft, episode_id="q-qlora", question="Explain QLoRA.", recorded_at_ms=2_000
    )

    assert rewards.effective_reward("q-qlora") == 1.0
    assert rewards.events_for("q-qlora")[0].source is RewardSource.MORNING_CURATION
    assert candidate.reasoning  # the SFT target carries reasoning, not just answer
    assert not draft_path.exists()  # promoted out of vault/inbox/**
    assert curated.exists()


@pytest.mark.asyncio
async def test_question_with_no_live_worker_is_skipped_not_lost(tmp_path: Path, transports) -> None:
    # No worker subscribed / registered → the question is reported skipped and
    # stays runnable for the next night rather than being silently dropped.
    _, coord, _worker = transports
    queue = QuestionQueue()
    queue.add(ResearchQuestion("q-orphan", "Explain RLHF.", SPECIALTY, created_at_ms=1))
    queue.approve("q-orphan")
    episodes = EpisodeStore()
    client = SubtaskDispatchClient(
        transport=coord, sender_id="coordinator", now_ms=_now_ms_factory()
    )
    dispatcher = NightlyDispatcher(
        queue=queue,
        dispatch_client=client,
        episode_store=episodes,
        registry=_registry(),  # nobody live
        now_ms=_now_ms_factory(),
        deadline_ms=10_000,
    )

    report = await dispatcher.run_nightly(batch_id="night-1")

    assert report.skipped == ("q-orphan",)
    assert report.dispatched == ()
