"""RemoteCritic + judge handler integration (issue #117)."""

from __future__ import annotations

import json

import pytest

from turing.coordinator.dispatch import (
    SubtaskDispatchClient,
    SubtaskKind,
    TaskResult,
)
from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.learning.critic.critic import CriticScore
from turing.learning.critic.remote import (
    JUDGE_SPECIALTY,
    RemoteCritic,
    episode_to_judge_payload,
    parse_critic_score,
)
from turing.transport.bus import InMemoryBus
from turing.transport.envelope import MeshMessage
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner
from turing.worker.executor.critic_handler import make_critic_handler


def _episode(
    *,
    subtask_id: str = "s-1",
    output_text: str = "the answer",
    outcome: SubtaskState = SubtaskState.COMPLETED,
) -> Episode:
    return Episode(
        task_id="t-1",
        subtask_id=subtask_id,
        worker_id="w",
        specialty="research-summarize",
        model_version="qwen2.5-7b",
        adapter_version="base",
        input_text="prompt",
        trajectory=("step",),
        output_text=output_text,
        success=outcome is SubtaskState.COMPLETED,
        latency_ms=1,
        tokens_used=1,
        outcome=outcome,
        critic_score=0.0,
        recorded_at_ms=0,
    )


def _transports():
    bus = InMemoryBus()
    coord_signer = MessageSigner.generate()
    judge_signer = MessageSigner.generate()
    coord = SignedTransport(
        bus=bus,
        signer=coord_signer,
        trusted_keys={"judge-1": judge_signer.public_key},
        now_ms=lambda: 1_000,
    )
    judge = SignedTransport(
        bus=bus,
        signer=judge_signer,
        trusted_keys={"coordinator": coord_signer.public_key},
        now_ms=lambda: 1_000,
    )
    return bus, coord, judge


# ── Plumbing ──────────────────────────────────────────────────────────


def test_episode_payload_includes_truncated_fields() -> None:
    ep = _episode(output_text="hello world")
    payload = episode_to_judge_payload(ep)
    assert payload["subtask_id"] == "s-1"
    assert payload["output_text"] == "hello world"
    assert payload["specialty"] == "research-summarize"


def test_parse_critic_score_round_trip() -> None:
    raw = json.dumps(
        {"correctness": 0.8, "efficiency": 0.6, "specialty_fit": 0.7, "critique": "ok"}
    )
    score = parse_critic_score(raw)
    assert score == CriticScore(correctness=0.8, efficiency=0.6, specialty_fit=0.7, critique="ok")


# ── RemoteCritic round-trip ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_remote_critic_dispatches_kind_critic_score_and_returns_score():
    _bus, coord, judge_t = _transports()

    captured: list[dict[str, object]] = []

    async def judge_handler(msg: MeshMessage) -> None:
        body = json.loads(msg.payload.decode("utf-8"))
        captured.append(body)
        score = {
            "correctness": 0.9,
            "efficiency": 0.8,
            "specialty_fit": 0.7,
            "critique": "fine",
        }
        result = TaskResult(
            subtask_id=body["subtask_id"],
            worker_id="judge-1",
            status="COMPLETED",
            output=json.dumps(score),
            tokens_used=10,
            latency_ms=5,
            model="haiku",
        )
        await judge_t.publish(
            MeshMessage(
                request_id=f"r-{body['subtask_id']}",
                sender_id="judge-1",
                subject=f"subtasks.{body['subtask_id']}.result",
                payload=json.dumps(result.to_dict()).encode("utf-8"),
                timestamp_ms=1_001,
            )
        )

    await judge_t.subscribe(f"subtasks.{JUDGE_SPECIALTY}", judge_handler)

    client = SubtaskDispatchClient(transport=coord, sender_id="coordinator", now_ms=lambda: 1_000)
    critic = RemoteCritic(dispatch_client=client, now_ms=lambda: 1_000)

    score = await critic.score(_episode())

    assert score.overall == pytest.approx((0.9 + 0.8 + 0.7) / 3)
    # Verify the dispatch carried kind=critic_score and specialty=judge.
    assert len(captured) == 1
    assert captured[0]["kind"] == SubtaskKind.CRITIC_SCORE.value
    assert captured[0]["specialty"] == JUDGE_SPECIALTY


# ── Judge handler ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_judge_handler_returns_low_score_for_empty_output():
    async def never_called(_payload: dict[str, object]) -> CriticScore:
        raise AssertionError("judge_fn must not be invoked for empty output")

    handler = make_critic_handler(never_called)
    envelope_payload = {
        "subtask_id": "s",
        "specialty": "x",
        "input_text": "i",
        "trajectory": [],
        "output_text": "",
        "outcome": "TIMED_OUT",
        "success": False,
    }

    class _Env:
        prompt = json.dumps(envelope_payload)

    result = await handler(_Env())  # type: ignore[arg-type]
    body = json.loads(result["output"])
    score = parse_critic_score(json.dumps(body))
    assert score.overall < 0.5
    assert "TIMED_OUT" in score.critique or "timed_out" in score.critique.lower()


@pytest.mark.asyncio
async def test_judge_handler_calls_judge_fn_for_non_empty_output():
    seen: list[dict[str, object]] = []

    async def judge_fn(payload: dict[str, object]) -> CriticScore:
        seen.append(payload)
        return CriticScore(correctness=0.6, efficiency=0.5, specialty_fit=0.7, critique="meh")

    handler = make_critic_handler(judge_fn)

    class _Env:
        prompt = json.dumps(
            {
                "subtask_id": "s",
                "specialty": "x",
                "input_text": "i",
                "trajectory": ["a"],
                "output_text": "real answer",
                "outcome": "COMPLETED",
                "success": True,
            }
        )

    result = await handler(_Env())  # type: ignore[arg-type]
    score = parse_critic_score(result["output"])
    assert score.correctness == pytest.approx(0.6)
    assert seen[0]["output_text"] == "real answer"
