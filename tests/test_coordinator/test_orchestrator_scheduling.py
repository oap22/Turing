"""Completion-driven scheduling and live-graph safety for issue #421."""

from __future__ import annotations

import asyncio
import gc

import pytest

from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.coordinator.orchestrator import DAGOrchestrator, WorkerOutput, _LiveDAG
from turing.coordinator.planner.schema import DAG, NeedsSubtaskFragment


def _subtask(
    subtask_id: str,
    specialty: str,
    *,
    depends_on: list[str] | None = None,
    inputs: dict[str, str] | None = None,
    task_id: str = "tsk_schedule",
) -> dict[str, object]:
    return {
        "id": subtask_id,
        "specialty_required": specialty,
        "prompt": subtask_id,
        "depends_on": depends_on or [],
        "required_tools": [],
        "inputs": inputs or {},
        "output_key": f"workspace://{task_id}/{subtask_id}/result",
    }


def _dag(*subtasks: dict[str, object], task_id: str = "tsk_schedule") -> DAG:
    return DAG.model_validate({"task_id": task_id, "version": 1, "subtasks": subtasks})


class _Synth:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def synthesize(self, *, user_prompt: str, leaf_outputs: dict[str, str]) -> WorkerOutput:
        self.calls.append(leaf_outputs)
        return WorkerOutput(output_text="final")


@pytest.mark.asyncio
async def test_dependent_node_starts_while_unrelated_node_is_blocked() -> None:
    a_started = asyncio.Event()
    b_started = asyncio.Event()
    c_started = asyncio.Event()
    d_started = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()
    seen_c_inputs: dict[str, str] = {}
    call_counts: dict[str, int] = {}

    class _Worker:
        async def execute(self, subtask, inputs):
            call_counts[subtask.id] = call_counts.get(subtask.id, 0) + 1
            if subtask.id == "st_a":
                a_started.set()
                await release_a.wait()
                return WorkerOutput(output_text="A")
            if subtask.id == "st_b":
                b_started.set()
                await release_b.wait()
                return WorkerOutput(output_text="B")
            if subtask.id == "st_c":
                c_started.set()
                seen_c_inputs.update(inputs)
                return WorkerOutput(output_text="C")
            d_started.set()
            assert inputs == {"c": "C"}
            return WorkerOutput(output_text="D")

    synth = _Synth()
    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={
            "research-discover": _Worker(),
            "research-deep": _Worker(),
            "research-summarize": _Worker(),
        },
        synthesizer=synth,
    )
    dag = _dag(
        _subtask("st_a", "research-discover"),
        _subtask("st_b", "research-deep"),
        _subtask(
            "st_c",
            "research-summarize",
            depends_on=["st_a"],
            inputs={"a": "workspace://tsk_schedule/st_a/result"},
        ),
        _subtask(
            "st_d",
            "research-summarize",
            depends_on=["st_c"],
            inputs={"c": "workspace://tsk_schedule/st_c/result"},
        ),
    )

    run_task = asyncio.create_task(orch.run(user_prompt="p", dag=dag, registry=None))
    await asyncio.wait_for(a_started.wait(), timeout=1)
    await asyncio.wait_for(b_started.wait(), timeout=1)
    release_a.set()
    await asyncio.wait_for(c_started.wait(), timeout=1)
    assert seen_c_inputs == {"a": "A"}
    await asyncio.wait_for(d_started.wait(), timeout=1)
    assert synth.calls == []
    release_b.set()
    assert await asyncio.wait_for(run_task, timeout=1) == "final"
    assert call_counts == {"st_a": 1, "st_b": 1, "st_c": 1, "st_d": 1}


@pytest.mark.asyncio
async def test_deferral_rejoins_after_designated_fragment_while_other_fragment_runs() -> None:
    fragment_started = asyncio.Event()
    unrelated_started = asyncio.Event()
    parent_resumed = asyncio.Event()
    release_unrelated = asyncio.Event()
    parent_inputs: list[dict[str, str]] = []

    fragment = NeedsSubtaskFragment.model_validate(
        {
            "subtasks": [
                _subtask("st_x", "research-deep", task_id="tsk_defer"),
                _subtask("st_y", "research-deep", task_id="tsk_defer"),
            ],
            "rejoin_after": ["st_x"],
        }
    )

    class _Worker:
        def __init__(self) -> None:
            self.parent_calls = 0

        async def execute(self, subtask, inputs):
            if subtask.id == "st_parent":
                self.parent_calls += 1
                parent_inputs.append(inputs)
                if self.parent_calls == 1:
                    return WorkerOutput(output_text="", needs_subtask=fragment)
                parent_resumed.set()
                return WorkerOutput(output_text="parent")
            if subtask.id == "st_seed":
                return WorkerOutput(output_text="seed")
            if subtask.id == "st_x":
                fragment_started.set()
                return WorkerOutput(output_text="x")
            unrelated_started.set()
            await release_unrelated.wait()
            return WorkerOutput(output_text="y")

    worker = _Worker()
    synth = _Synth()
    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={
            "research-discover": worker,
            "research-summarize": worker,
            "research-deep": worker,
        },
        synthesizer=synth,
    )
    dag = _dag(
        _subtask("st_seed", "research-discover", task_id="tsk_defer"),
        _subtask(
            "st_parent",
            "research-summarize",
            depends_on=["st_seed"],
            inputs={"original": "workspace://tsk_defer/st_seed/result"},
            task_id="tsk_defer",
        ),
        task_id="tsk_defer",
    )
    run_task = asyncio.create_task(orch.run(user_prompt="p", dag=dag, registry=None))
    await asyncio.wait_for(fragment_started.wait(), timeout=1)
    await asyncio.wait_for(unrelated_started.wait(), timeout=1)
    await asyncio.wait_for(parent_resumed.wait(), timeout=1)
    assert worker.parent_calls == 2
    assert parent_inputs == [
        {"original": "seed"},
        {"original": "seed", "st_x": "x"},
    ]
    assert synth.calls == []
    release_unrelated.set()
    assert await asyncio.wait_for(run_task, timeout=1) == "final"
    assert set(synth.calls[0].values()) == {"parent", "y"}


@pytest.mark.asyncio
async def test_parent_can_request_multiple_fragments_sequentially() -> None:
    first_fragment = NeedsSubtaskFragment.model_validate(
        {
            "subtasks": [_subtask("st_first", "research-deep", task_id="tsk_multi")],
            "rejoin_after": ["st_first"],
        }
    )
    second_fragment = NeedsSubtaskFragment.model_validate(
        {
            "subtasks": [_subtask("st_second", "research-deep", task_id="tsk_multi")],
            "rejoin_after": ["st_second"],
        }
    )

    class _Worker:
        def __init__(self) -> None:
            self.parent_calls = 0
            self.active_parent_calls = 0
            self.max_active_parent_calls = 0

        async def execute(self, subtask, inputs):
            if subtask.id != "st_parent":
                return WorkerOutput(output_text=subtask.id)
            self.parent_calls += 1
            self.active_parent_calls += 1
            self.max_active_parent_calls = max(
                self.max_active_parent_calls, self.active_parent_calls
            )
            try:
                if self.parent_calls == 1:
                    return WorkerOutput(output_text="", needs_subtask=first_fragment)
                if self.parent_calls == 2:
                    assert inputs == {"st_first": "st_first"}
                    return WorkerOutput(output_text="", needs_subtask=second_fragment)
                assert inputs == {"st_first": "st_first", "st_second": "st_second"}
                return WorkerOutput(output_text="parent")
            finally:
                self.active_parent_calls -= 1

    worker = _Worker()
    synth = _Synth()
    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={"research-summarize": worker, "research-deep": worker},
        synthesizer=synth,
    )
    dag = _dag(
        _subtask("st_parent", "research-summarize", task_id="tsk_multi"), task_id="tsk_multi"
    )
    assert (
        await asyncio.wait_for(orch.run(user_prompt="p", dag=dag, registry=None), timeout=1)
        == "final"
    )
    assert worker.parent_calls == 3
    assert worker.max_active_parent_calls == 1


@pytest.mark.asyncio
async def test_duplicate_initial_output_uri_rejected_before_dispatch() -> None:
    calls = 0

    class _Worker:
        async def execute(self, subtask, inputs):
            nonlocal calls
            calls += 1
            return WorkerOutput(output_text="unexpected")

    duplicate_uri = "workspace://tsk_duplicate/result"
    dag = DAG.model_validate(
        {
            "task_id": "tsk_duplicate",
            "version": 1,
            "subtasks": [
                _subtask("st_a", "research-discover", task_id="tsk_duplicate")
                | {"output_key": duplicate_uri},
                _subtask("st_b", "research-deep", task_id="tsk_duplicate")
                | {"output_key": duplicate_uri},
            ],
        }
    )
    synth = _Synth()
    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={"research-discover": _Worker(), "research-deep": _Worker()},
        synthesizer=synth,
    )
    with pytest.raises(ValueError, match="duplicate output_key"):
        await orch.run(user_prompt="p", dag=dag, registry=None)
    assert calls == 0
    assert synth.calls == []


@pytest.mark.asyncio
async def test_failure_cancels_blocked_sibling_and_skips_synthesis() -> None:
    sibling_cancelled = asyncio.Event()
    sibling_started = asyncio.Event()

    class _Failing:
        async def execute(self, subtask, inputs):
            raise RuntimeError("original failure")

    class _Blocked:
        async def execute(self, subtask, inputs):
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    synth = _Synth()
    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={"research-discover": _Failing(), "research-deep": _Blocked()},
        synthesizer=synth,
    )
    dag = _dag(_subtask("st_a", "research-discover"), _subtask("st_b", "research-deep"))
    with pytest.raises(RuntimeError, match="original failure"):
        await asyncio.wait_for(orch.run(user_prompt="p", dag=dag, registry=None), timeout=1)
    assert sibling_started.is_set()
    assert sibling_cancelled.is_set()
    assert synth.calls == []


@pytest.mark.asyncio
async def test_external_cancellation_cleans_up_all_owned_workers() -> None:
    cancelled: set[str] = set()
    started: set[str] = set()
    all_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    class _Blocked:
        async def execute(self, subtask, inputs):
            started.add(subtask.id)
            if len(started) == 2:
                all_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                cancelled.add(subtask.id)

    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={"research-discover": _Blocked(), "research-deep": _Blocked()},
        synthesizer=_Synth(),
    )
    dag = _dag(_subtask("st_a", "research-discover"), _subtask("st_b", "research-deep"))
    run_task = asyncio.create_task(orch.run(user_prompt="p", dag=dag, registry=None))
    await asyncio.wait_for(all_started.wait(), timeout=1)
    run_task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    assert not run_task.done()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run_task, timeout=1)
    assert cancelled == {"st_a", "st_b"}


def test_fragment_validation_is_atomic_for_late_id_collision() -> None:
    dag = _dag(
        _subtask("st_parent", "research-summarize"),
        _subtask("st_existing", "research-deep"),
    )
    live = _LiveDAG.from_dag(dag)
    before = dict(live.subtasks)
    fragment = NeedsSubtaskFragment.model_validate(
        {
            "subtasks": [
                _subtask("st_new", "research-deep", task_id="tsk_schedule"),
                _subtask("st_existing", "research-deep", task_id="tsk_schedule"),
            ],
            "rejoin_after": ["st_new"],
        }
    )

    with pytest.raises(ValueError, match="collides with existing subtask"):
        live.add_fragment(fragment, rejoin_target="st_parent")
    assert live.subtasks == before


@pytest.mark.asyncio
async def test_same_batch_success_and_failure_never_dispatches_dependent() -> None:
    a_started = asyncio.Event()
    b_started = asyncio.Event()
    a_release = asyncio.Event()
    b_release = asyncio.Event()
    c_started = asyncio.Event()

    class _Worker:
        async def execute(self, subtask, inputs):
            if subtask.id == "st_a":
                a_started.set()
                await a_release.wait()
                return WorkerOutput(output_text="A")
            if subtask.id == "st_b":
                b_started.set()
                await b_release.wait()
                raise RuntimeError("batch failure")
            c_started.set()
            return WorkerOutput(output_text="unexpected")

    synth = _Synth()
    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={
            "research-discover": _Worker(),
            "research-deep": _Worker(),
            "research-summarize": _Worker(),
        },
        synthesizer=synth,
    )
    dag = _dag(
        _subtask("st_a", "research-discover"),
        _subtask("st_b", "research-deep"),
        _subtask(
            "st_c",
            "research-summarize",
            depends_on=["st_a"],
            inputs={"a": "workspace://tsk_schedule/st_a/result"},
        ),
    )
    run_task = asyncio.create_task(orch.run(user_prompt="p", dag=dag, registry=None))
    await asyncio.wait_for(asyncio.gather(a_started.wait(), b_started.wait()), timeout=1)
    a_release.set()
    b_release.set()
    with pytest.raises(RuntimeError, match="batch failure"):
        await asyncio.wait_for(run_task, timeout=1)
    assert not c_started.is_set()
    assert synth.calls == []


@pytest.mark.asyncio
async def test_diamond_fan_in_waits_for_both_inputs_and_invokes_each_once() -> None:
    a_started = asyncio.Event()
    b_started = asyncio.Event()
    a_finished = asyncio.Event()
    b_finished = asyncio.Event()
    c_started = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()
    calls: dict[str, int] = {}
    c_inputs: dict[str, str] = {}

    class _Worker:
        async def execute(self, subtask, inputs):
            calls[subtask.id] = calls.get(subtask.id, 0) + 1
            if subtask.id == "st_a":
                a_started.set()
                await release_a.wait()
                a_finished.set()
                return WorkerOutput(output_text="A")
            if subtask.id == "st_b":
                b_started.set()
                await release_b.wait()
                b_finished.set()
                return WorkerOutput(output_text="B")
            c_started.set()
            c_inputs.update(inputs)
            return WorkerOutput(output_text="C")

    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={
            "research-discover": _Worker(),
            "research-deep": _Worker(),
            "research-summarize": _Worker(),
        },
        synthesizer=_Synth(),
    )
    dag = _dag(
        _subtask("st_a", "research-discover"),
        _subtask("st_b", "research-deep"),
        _subtask(
            "st_c",
            "research-summarize",
            depends_on=["st_a", "st_b"],
            inputs={
                "a": "workspace://tsk_schedule/st_a/result",
                "b": "workspace://tsk_schedule/st_b/result",
            },
        ),
    )
    run_task = asyncio.create_task(orch.run(user_prompt="p", dag=dag, registry=None))
    await asyncio.wait_for(asyncio.gather(a_started.wait(), b_started.wait()), timeout=1)
    release_a.set()
    await asyncio.wait_for(a_finished.wait(), timeout=1)
    assert not c_started.is_set()
    release_b.set()
    await asyncio.wait_for(b_finished.wait(), timeout=1)
    assert await asyncio.wait_for(run_task, timeout=1) == "final"
    assert c_inputs == {"a": "A", "b": "B"}
    assert calls == {"st_a": 1, "st_b": 1, "st_c": 1}


@pytest.mark.asyncio
async def test_parent_input_alias_collision_is_atomic_and_cleans_sibling() -> None:
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    parent_calls = 0

    fragment = NeedsSubtaskFragment.model_validate(
        {
            "subtasks": [_subtask("st_x", "research-deep", task_id="tsk_alias")],
            "rejoin_after": ["st_x"],
        }
    )

    class _Worker:
        async def execute(self, subtask, inputs):
            nonlocal parent_calls
            if subtask.id == "st_dep":
                return WorkerOutput(output_text="dep")
            if subtask.id == "st_parent":
                parent_calls += 1
                return WorkerOutput(output_text="", needs_subtask=fragment)
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    synth = _Synth()
    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={
            "research-discover": _Worker(),
            "research-summarize": _Worker(),
            "research-deep": _Worker(),
        },
        synthesizer=synth,
    )
    dep_uri = "workspace://tsk_alias/st_dep/result"
    dag = _dag(
        _subtask("st_dep", "research-discover", task_id="tsk_alias"),
        _subtask(
            "st_parent",
            "research-summarize",
            depends_on=["st_dep"],
            inputs={"st_x": dep_uri},
            task_id="tsk_alias",
        ),
        _subtask("st_sibling", "research-deep", task_id="tsk_alias"),
        task_id="tsk_alias",
    )
    with pytest.raises(ValueError, match="parent input"):
        await asyncio.wait_for(orch.run(user_prompt="p", dag=dag, registry=None), timeout=1)
    assert parent_calls == 1
    assert sibling_started.is_set()
    assert sibling_cancelled.is_set()
    assert synth.calls == []


def test_fragment_output_uri_collisions_are_atomic() -> None:
    dag = _dag(
        _subtask("st_parent", "research-summarize"),
        _subtask("st_existing", "research-deep"),
    )
    live = _LiveDAG.from_dag(dag)
    before = dict(live.subtasks)
    existing_uri = live.subtasks["st_existing"].output_key
    for fragment_subtasks, message in (
        (
            [
                _subtask("st_first", "research-deep"),
                _subtask("st_second", "research-deep")
                | {"output_key": "workspace://tsk_schedule/duplicate/result"},
            ],
            "duplicate NEEDS_SUBTASK fragment output_key",
        ),
        (
            [_subtask("st_new", "research-deep") | {"output_key": existing_uri}],
            "collides with existing subtask",
        ),
    ):
        if message.startswith("duplicate"):
            fragment_subtasks[1]["output_key"] = fragment_subtasks[0]["output_key"]
        fragment = NeedsSubtaskFragment.model_validate(
            {"subtasks": fragment_subtasks, "rejoin_after": [fragment_subtasks[0]["id"]]}
        )
        with pytest.raises(ValueError, match=message):
            live.add_fragment(fragment, rejoin_target="st_parent")
        assert live.subtasks == before


@pytest.mark.asyncio
async def test_overlapping_fragments_abort_and_cancel_unrelated_owned_task() -> None:
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    fragment_started = asyncio.Event()
    shared_fragment = NeedsSubtaskFragment.model_validate(
        {
            "subtasks": [_subtask("st_shared", "research-deep", task_id="tsk_overlap")],
            "rejoin_after": ["st_shared"],
        }
    )

    class _Worker:
        async def execute(self, subtask, inputs):
            if subtask.id in {"st_one", "st_two"}:
                return WorkerOutput(output_text="", needs_subtask=shared_fragment)
            if subtask.id == "st_shared":
                fragment_started.set()
                return WorkerOutput(output_text="unexpected")
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    synth = _Synth()
    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={"research-summarize": _Worker(), "research-deep": _Worker()},
        synthesizer=synth,
    )
    dag = _dag(
        _subtask("st_one", "research-summarize", task_id="tsk_overlap"),
        _subtask("st_two", "research-summarize", task_id="tsk_overlap"),
        _subtask("st_sibling", "research-deep", task_id="tsk_overlap"),
        task_id="tsk_overlap",
    )
    with pytest.raises(ValueError, match="collides with existing subtask"):
        await asyncio.wait_for(orch.run(user_prompt="p", dag=dag, registry=None), timeout=1)
    assert sibling_started.is_set()
    assert sibling_cancelled.is_set()
    assert not fragment_started.is_set()
    assert synth.calls == []


@pytest.mark.asyncio
async def test_simultaneous_failures_use_graph_order_and_are_consumed() -> None:
    a_started = asyncio.Event()
    b_started = asyncio.Event()
    release = asyncio.Event()
    unhandled: list[dict[str, object]] = []

    class _Worker:
        async def execute(self, subtask, inputs):
            if subtask.id == "st_a":
                a_started.set()
                await release.wait()
                raise RuntimeError("error from a")
            b_started.set()
            await release.wait()
            raise RuntimeError("error from b")

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        orch = DAGOrchestrator(
            episode_store=EpisodeStore(),
            worker_for_specialty={"research-discover": _Worker(), "research-deep": _Worker()},
            synthesizer=_Synth(),
        )
        dag = _dag(
            _subtask("st_a", "research-discover"),
            _subtask("st_b", "research-deep"),
        )
        run_task = asyncio.create_task(orch.run(user_prompt="p", dag=dag, registry=None))
        await asyncio.wait_for(asyncio.gather(a_started.wait(), b_started.wait()), timeout=1)
        release.set()
        with pytest.raises(RuntimeError, match="error from a"):
            await asyncio.wait_for(run_task, timeout=1)
        del run_task
        gc.collect()
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_worker_self_cancellation_aborts_run_and_cleans_sibling() -> None:
    sibling_cancelled = asyncio.Event()

    class _Worker:
        async def execute(self, subtask, inputs):
            if subtask.id == "st_a":
                raise asyncio.CancelledError("worker cancelled itself")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    synth = _Synth()
    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={"research-discover": _Worker(), "research-deep": _Worker()},
        synthesizer=synth,
    )
    dag = _dag(_subtask("st_a", "research-discover"), _subtask("st_b", "research-deep"))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(orch.run(user_prompt="p", dag=dag, registry=None), timeout=1)
    assert sibling_cancelled.is_set()
    assert synth.calls == []


@pytest.mark.asyncio
async def test_defensive_stall_reports_unresolved_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    dag = _dag(_subtask("st_a", "research-discover"))
    original_from_dag = _LiveDAG.from_dag

    def _mutated_from_dag(dag: DAG) -> _LiveDAG:
        live = original_from_dag(dag)
        live.subtasks["st_a"] = live.subtasks["st_a"].model_copy(
            update={"depends_on": ["st_missing"]}
        )
        return live

    monkeypatch.setattr(_LiveDAG, "from_dag", _mutated_from_dag)

    class _Worker:
        async def execute(self, subtask, inputs):
            raise AssertionError("stalled graph must not dispatch")

    orch = DAGOrchestrator(
        episode_store=EpisodeStore(),
        worker_for_specialty={"research-discover": _Worker()},
        synthesizer=_Synth(),
    )
    with pytest.raises(RuntimeError, match=r"stalled.*st_a"):
        await orch.run(user_prompt="p", dag=dag, registry=None)
