"""DAGOrchestrator — runs a validated DAG end-to-end.

Slice 7/26 (#9), AC2/AC3/AC4. Given a :class:`DAG` and per-specialty
worker clients, it:

  1. Dispatches independent subtasks in parallel and waits for all of
     them, respecting ``depends_on`` for fan-in.
  2. After all DAG subtasks complete, runs a synthesis pass over the
     leaf outputs to produce a single final reply.
  3. Records four episodes (planner + each subtask + synthesis) into
     the :class:`EpisodeStore`, all tagged with the same ``task_id``.
  4. If a worker returns a ``NEEDS_SUBTASK`` fragment, splices the
     fragment's subtasks into the live DAG and dispatches them before
     allowing the worker's parent subtask to be re-attempted.

The orchestrator is intentionally deep: callers pass workers and an
episode store, and get back a final-reply string. The lifecycle, retry,
and capability-check details stay inside this module.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import structlog

from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState

if TYPE_CHECKING:
    from turing.coordinator.lifecycle.episode_store import EpisodeStore
    from turing.coordinator.planner.schema import (
        DAG,
        NeedsSubtaskFragment,
        Subtask,
    )
    from turing.coordinator.registry import CapabilityRegistry


logger = structlog.get_logger("turing.coordinator.orchestrator")


@dataclass(frozen=True)
class WorkerOutput:
    """What a worker returns from a single subtask attempt."""

    output_text: str
    latency_ms: int = 0
    tokens_used: int = 0
    needs_subtask: NeedsSubtaskFragment | None = None


class WorkerClient(Protocol):
    async def execute(
        self,
        subtask: Subtask,
        inputs: dict[str, str],
    ) -> WorkerOutput: ...


class Synthesizer(Protocol):
    async def synthesize(
        self,
        *,
        user_prompt: str,
        leaf_outputs: dict[str, str],
    ) -> WorkerOutput: ...


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class _LiveDAG:
    """Mutable working copy of a DAG, supporting NEEDS_SUBTASK splicing."""

    task_id: str
    subtasks: dict[str, Subtask] = field(default_factory=dict)

    @classmethod
    def from_dag(cls, dag: DAG) -> _LiveDAG:
        return cls(
            task_id=dag.task_id,
            subtasks={s.id: s for s in dag.subtasks},
        )

    def leaves(self) -> list[str]:
        consumed: set[str] = set()
        for s in self.subtasks.values():
            consumed.update(s.depends_on)
        return [sid for sid in self.subtasks if sid not in consumed]

    def add_fragment(
        self,
        fragment: NeedsSubtaskFragment,
        *,
        rejoin_target: str,
    ) -> list[str]:
        """Splice a NEEDS_SUBTASK fragment into the DAG.

        New subtasks are added; ``rejoin_target`` (the subtask that asked
        for help) gains ``depends_on`` edges to the fragment's
        ``rejoin_after`` ids so it won't run again until the fragment
        completes.
        """
        new_ids: list[str] = []
        for sub in fragment.subtasks:
            if sub.id in self.subtasks:
                raise ValueError(
                    f"NEEDS_SUBTASK fragment id {sub.id!r} collides with existing subtask"
                )
            self.subtasks[sub.id] = sub
            new_ids.append(sub.id)

        target = self.subtasks[rejoin_target]
        merged_deps = list(dict.fromkeys([*target.depends_on, *fragment.rejoin_after]))
        # Auto-wire each rejoin_after producer's output into the parent's
        # inputs so the resumed worker sees the fragment's results.
        merged_inputs = dict(target.inputs)
        for sid in fragment.rejoin_after:
            merged_inputs.setdefault(sid, self.subtasks[sid].output_key)
        self.subtasks[rejoin_target] = target.model_copy(
            update={"depends_on": merged_deps, "inputs": merged_inputs}
        )
        return new_ids


class DAGOrchestrator:
    def __init__(
        self,
        *,
        episode_store: EpisodeStore,
        worker_for_specialty: dict[str, WorkerClient],
        synthesizer: Synthesizer,
        now_ms=_now_ms,
    ) -> None:
        self._store = episode_store
        self._workers = worker_for_specialty
        self._synth = synthesizer
        self._now_ms = now_ms

    async def run(
        self,
        *,
        user_prompt: str,
        dag: DAG,
        registry: CapabilityRegistry | None,
    ) -> str:
        """Run the DAG to completion and return the synthesized reply.

        ``registry`` is accepted for parity with the planning step but is
        not strictly required here — the DAG is assumed pre-validated.
        It will be used in future slices for live capability checks.
        """
        del registry  # reserved for future live re-checks

        live = _LiveDAG.from_dag(dag)
        outputs: dict[str, str] = {}
        completed: set[str] = set()

        # Record the planner's own episode first — small but real lineage.
        self._store.record(
            Episode(
                task_id=live.task_id,
                subtask_id="st_planner",
                worker_id="planner",
                specialty="planner",
                model_version="",
                adapter_version="",
                input_text=user_prompt,
                trajectory=tuple(s.id for s in dag.subtasks),
                output_text=f"DAG with {len(dag.subtasks)} subtasks",
                success=True,
                latency_ms=0,
                tokens_used=0,
                outcome=SubtaskState.COMPLETED,
                critic_score=0.0,
                recorded_at_ms=self._now_ms(),
            )
        )

        # Iterate in waves: every subtask whose deps are all completed runs
        # concurrently. Loop continues if NEEDS_SUBTASK adds new subtasks.
        while True:
            ready = [
                live.subtasks[sid]
                for sid in live.subtasks
                if sid not in completed
                and all(d in completed for d in live.subtasks[sid].depends_on)
            ]
            if not ready:
                break

            results = await asyncio.gather(
                *(self._run_one(s, outputs, live) for s in ready),
                return_exceptions=True,
            )
            for st, res in zip(ready, results, strict=True):
                if isinstance(res, BaseException):
                    raise res
                if res is _NEEDS_SUBTASK_DEFERRED:
                    # st was deferred — its dependencies (the new fragment
                    # subtasks) need to run first.
                    continue
                outputs[st.output_key] = res
                completed.add(st.id)

        leaf_outputs = {sid: outputs[live.subtasks[sid].output_key] for sid in live.leaves()}

        synth_started = self._now_ms()
        synth_out = await self._synth.synthesize(
            user_prompt=user_prompt,
            leaf_outputs=leaf_outputs,
        )
        self._store.record(
            Episode(
                task_id=live.task_id,
                subtask_id="st_synthesis",
                worker_id="synthesizer",
                specialty="synthesis",
                model_version="",
                adapter_version="",
                input_text=user_prompt,
                trajectory=tuple(leaf_outputs.keys()),
                output_text=synth_out.output_text,
                success=True,
                latency_ms=self._now_ms() - synth_started,
                tokens_used=synth_out.tokens_used,
                outcome=SubtaskState.COMPLETED,
                critic_score=0.0,
                recorded_at_ms=self._now_ms(),
            )
        )
        return synth_out.output_text

    async def _run_one(
        self,
        subtask: Subtask,
        outputs: dict[str, str],
        live: _LiveDAG,
    ) -> str | object:
        specialty = subtask.specialty_required.value
        worker = self._workers.get(specialty)
        if worker is None:
            raise RuntimeError(
                f"no worker registered for specialty {specialty!r} (subtask {subtask.id!r})"
            )

        resolved_inputs = {key: outputs[uri] for key, uri in subtask.inputs.items()}

        started = self._now_ms()
        try:
            out = await worker.execute(subtask, resolved_inputs)
        except Exception as exc:
            self._store.record(
                Episode(
                    task_id=live.task_id,
                    subtask_id=subtask.id,
                    worker_id=specialty,
                    specialty=specialty,
                    model_version="",
                    adapter_version="",
                    input_text=subtask.prompt,
                    trajectory=(),
                    output_text=str(exc),
                    success=False,
                    latency_ms=self._now_ms() - started,
                    tokens_used=0,
                    outcome=SubtaskState.FAILED,
                    critic_score=0.0,
                    recorded_at_ms=self._now_ms(),
                )
            )
            raise

        if out.needs_subtask is not None:
            # Splice fragment in; this subtask will be retried after its new
            # deps complete. Don't write a terminal episode yet.
            live.add_fragment(out.needs_subtask, rejoin_target=subtask.id)
            return _NEEDS_SUBTASK_DEFERRED

        self._store.record(
            Episode(
                task_id=live.task_id,
                subtask_id=subtask.id,
                worker_id=specialty,
                specialty=specialty,
                model_version="",
                adapter_version="",
                input_text=subtask.prompt,
                trajectory=tuple(resolved_inputs.values()),
                output_text=out.output_text,
                success=True,
                latency_ms=out.latency_ms or (self._now_ms() - started),
                tokens_used=out.tokens_used,
                outcome=SubtaskState.COMPLETED,
                critic_score=0.0,
                recorded_at_ms=self._now_ms(),
            )
        )
        return out.output_text


_NEEDS_SUBTASK_DEFERRED = object()
