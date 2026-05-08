"""Tests for SubtaskDispatch `kind` field + KindRouter (issue #113)."""

from __future__ import annotations

import pytest

from turing.coordinator.dispatch import SourceInput, SubtaskDispatch
from turing.coordinator.dispatch.envelopes import SubtaskKind
from turing.worker.executor.kind_router import KindRouter


def _envelope(kind: str = "default") -> SubtaskDispatch:
    return SubtaskDispatch(
        subtask_id="st_1",
        task_id="t_1",
        specialty="research-summarize",
        prompt="x",
        source_inputs=[SourceInput(id="s", text="t")],
        deadline_ms=1,
        kind=kind,
    )


def test_subtask_kind_enum_values_are_stable_strings():
    assert SubtaskKind.DEFAULT.value == "default"
    assert SubtaskKind.CRITIC_SCORE.value == "critic_score"
    assert SubtaskKind.CANARY_EVAL.value == "canary_eval"
    assert SubtaskKind.EXTRACT_LESSONS.value == "extract_lessons"


def test_envelope_default_kind_is_default():
    env = SubtaskDispatch(
        subtask_id="s",
        task_id="t",
        specialty="x",
        prompt="p",
        source_inputs=[],
        deadline_ms=1,
    )
    assert env.kind == "default"


def test_envelope_round_trips_non_default_kind():
    env = _envelope(kind="critic_score")
    d = env.to_dict()
    assert d["kind"] == "critic_score"
    assert SubtaskDispatch.from_dict(d) == env


def test_envelope_legacy_v1_payload_decodes_to_default_kind():
    legacy = {
        "version": 1,
        "subtask_id": "st_1",
        "task_id": "t_1",
        "specialty": "research-summarize",
        "prompt": "x",
        "source_inputs": [],
        "deadline_ms": 1,
        "capability_token": None,
        # no `kind` field
    }
    env = SubtaskDispatch.from_dict(legacy)
    assert env.kind == "default"


@pytest.mark.asyncio
async def test_kind_router_dispatches_to_registered_handler():
    router = KindRouter()

    async def critic(env):
        return {"handled_by": "critic", "subtask_id": env.subtask_id}

    async def default(env):
        return {"handled_by": "default"}

    router.register("default", default)
    router.register(SubtaskKind.CRITIC_SCORE.value, critic)

    res = await router.dispatch(_envelope(kind="critic_score"))
    assert res == {"handled_by": "critic", "subtask_id": "st_1"}


@pytest.mark.asyncio
async def test_kind_router_default_kind_uses_default_handler():
    router = KindRouter()

    async def default(env):
        return {"handled_by": "default"}

    router.register("default", default)
    res = await router.dispatch(_envelope(kind="default"))
    assert res == {"handled_by": "default"}


@pytest.mark.asyncio
async def test_kind_router_unknown_kind_returns_clean_error_result():
    router = KindRouter()

    async def default(env):
        return {"handled_by": "default"}

    router.register("default", default)
    res = await router.dispatch(_envelope(kind="who_knows"))
    assert res == {"status": "unknown_kind", "kind": "who_knows"}


@pytest.mark.asyncio
async def test_kind_router_missing_default_handler_unknown_kind_still_clean():
    router = KindRouter()
    # No default registered — unknown kind still returns clean dict.
    res = await router.dispatch(_envelope(kind="anything"))
    assert res == {"status": "unknown_kind", "kind": "anything"}
