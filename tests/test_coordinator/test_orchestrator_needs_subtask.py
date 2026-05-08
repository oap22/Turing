"""NEEDS_SUBTASK extension — slice 7/26 (#9), AC4.

When a worker discovers mid-run that it needs an upstream specialty it
can't satisfy itself, it returns a ``NeedsSubtaskFragment``. The
orchestrator splices the fragment into the live DAG, runs the new
subtask(s), then re-attempts the parent worker with the fresh inputs.
"""

from __future__ import annotations

import pytest

from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.orchestrator import DAGOrchestrator, WorkerOutput
from turing.coordinator.planner.schema import DAG, NeedsSubtaskFragment


def _dag_one_summarize_subtask() -> DAG:
    return DAG.model_validate(
        {
            "task_id": "tsk_ext",
            "version": 1,
            "subtasks": [
                {
                    "id": "st_sum",
                    "specialty_required": "research-summarize",
                    "prompt": "summarize the literature",
                    "depends_on": [],
                    "required_tools": [],
                    "inputs": {},
                    "output_key": "workspace://tsk_ext/st_sum/result",
                }
            ],
        }
    )


class _OneShotNeedsSubtaskWorker:
    """First call returns NEEDS_SUBTASK; second call succeeds."""

    def __init__(self, fragment: NeedsSubtaskFragment) -> None:
        self.fragment = fragment
        self.calls = 0

    async def execute(self, subtask, inputs):
        self.calls += 1
        if self.calls == 1:
            return WorkerOutput(
                output_text="",
                needs_subtask=self.fragment,
            )
        return WorkerOutput(
            output_text="summary using " + ",".join(inputs.values()),
        )


class _SimpleWorker:
    async def execute(self, subtask, inputs):
        return WorkerOutput(output_text=f"deep-results-for:{subtask.id}")


class _StubSynth:
    async def synthesize(self, *, user_prompt, leaf_outputs):
        return WorkerOutput(output_text="FINAL: " + " | ".join(leaf_outputs.values()))


class TestNeedsSubtaskExtension:
    @pytest.mark.asyncio
    async def test_fragment_runs_then_parent_resumes(self):
        fragment = NeedsSubtaskFragment.model_validate(
            {
                "subtasks": [
                    {
                        "id": "st_dive",
                        "specialty_required": "research-deep",
                        "prompt": "do the deep read",
                        "depends_on": [],
                        "required_tools": [],
                        "inputs": {},
                        "output_key": "workspace://tsk_ext/st_dive/result",
                    }
                ],
                "rejoin_after": ["st_dive"],
            }
        )

        store = EpisodeStore()
        sum_worker = _OneShotNeedsSubtaskWorker(fragment)
        deep_worker = _SimpleWorker()
        orch = DAGOrchestrator(
            episode_store=store,
            worker_for_specialty={
                "research-summarize": sum_worker,
                "research-deep": deep_worker,
            },
            synthesizer=_StubSynth(),
        )

        reply = await orch.run(
            user_prompt="research X",
            dag=_dag_one_summarize_subtask(),
            registry=None,
        )

        assert reply == "FINAL: summary using deep-results-for:st_dive"
        assert sum_worker.calls == 2  # initial + post-fragment retry

        # Episodes: planner + st_dive + st_sum + synthesis = 4
        all_eps = (
            store.query(specialty="planner")
            + store.query(specialty="research-deep")
            + store.query(specialty="research-summarize")
            + store.query(specialty="synthesis")
        )
        assert {ep.subtask_id for ep in all_eps} == {
            "st_planner",
            "st_dive",
            "st_sum",
            "st_synthesis",
        }
        assert all(ep.outcome is SubtaskState.COMPLETED for ep in all_eps)
        assert {ep.task_id for ep in all_eps} == {"tsk_ext"}
