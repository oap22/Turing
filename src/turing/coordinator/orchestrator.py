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

from turing.coordinator.dispatch import SourceInput, SubtaskDispatch
from turing.coordinator.dispatch.client import SubtaskTimeoutError
from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.scheduler.scheduler import Subtask as SchedulerSubtask

if TYPE_CHECKING:
    from collections.abc import Callable

    from turing.coordinator.capability_token import TokenIssuer
    from turing.coordinator.dispatch.client import SubtaskDispatchClient
    from turing.coordinator.lifecycle.episode_store import EpisodeStore
    from turing.coordinator.planner.schema import (
        DAG,
        NeedsSubtaskFragment,
        Subtask,
    )
    from turing.coordinator.registry import CapabilityRegistry
    from turing.coordinator.registry.manifest import CapabilityManifest
    from turing.coordinator.scheduler.scheduler import Scheduler


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
        now_ms: Callable[[], int] = _now_ms,
        dispatch_client: SubtaskDispatchClient | None = None,
        scheduler: Scheduler | None = None,
        token_issuer: TokenIssuer | None = None,
    ) -> None:
        self._store = episode_store
        self._workers = worker_for_specialty
        self._synth = synthesizer
        self._now_ms = now_ms
        self._dispatch_client = dispatch_client
        self._scheduler = scheduler
        self._token_issuer = token_issuer

    async def run(
        self,
        *,
        user_prompt: str,
        dag: DAG,
        registry: CapabilityRegistry | None,
    ) -> str:
        """Run the DAG to completion and return the synthesized reply.

        When constructed with both a ``scheduler`` and ``dispatch_client``,
        the orchestrator consults ``registry`` per subtask: locally-resident
        workers run in-process via ``worker_for_specialty``; remote workers
        are dispatched over the bus and awaited.
        """
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
                *(self._run_one(s, outputs, live, registry) for s in ready),
                return_exceptions=True,
            )
            for st, res in zip(ready, results, strict=True):
                if isinstance(res, BaseException):
                    raise res
                if res is _NEEDS_SUBTASK_DEFERRED:
                    # st was deferred — its dependencies (the new fragment
                    # subtasks) need to run first.
                    continue
                outputs[st.output_key] = str(res)
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
        registry: CapabilityRegistry | None,
    ) -> str | object:
        specialty = subtask.specialty_required.value
        resolved_inputs = {key: outputs[uri] for key, uri in subtask.inputs.items()}

        # Remote dispatch path: fully wired only when scheduler + dispatch
        # client + registry are all available. Otherwise fall through to the
        # in-process worker dict — preserving the pre-#95 local-only mode.
        if (
            self._dispatch_client is not None
            and self._scheduler is not None
            and registry is not None
        ):
            sched_st = SchedulerSubtask(
                subtask_id=subtask.id,
                specialty_required=specialty,
                required_tools=tuple(subtask.required_tools),
            )
            picked = self._scheduler.pick(sched_st, registry)
            if picked is not None and not registry.is_local(picked.worker_id):
                return await self._run_remote(
                    subtask=subtask,
                    sched_st=sched_st,
                    initial_pick=picked,
                    resolved_inputs=resolved_inputs,
                    registry=registry,
                    live=live,
                )

        worker = self._workers.get(specialty)
        if worker is None:
            raise RuntimeError(
                f"no worker registered for specialty {specialty!r} (subtask {subtask.id!r})"
            )

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

    async def _run_remote(
        self,
        *,
        subtask: Subtask,
        sched_st: SchedulerSubtask,
        initial_pick: CapabilityManifest,
        resolved_inputs: dict[str, str],
        registry: CapabilityRegistry,
        live: _LiveDAG,
    ) -> str:
        assert self._dispatch_client is not None
        assert self._scheduler is not None

        specialty = subtask.specialty_required.value
        deadline_ms = self._now_ms() + subtask.timeout_s * 1000
        tried: list[str] = []

        pick: CapabilityManifest | None = initial_pick
        last_error = "no worker available"
        last_outcome = SubtaskState.FAILED

        # AC4: dispatch once; on FAILED/TIMED_OUT, pick_for_retry and
        # re-dispatch exactly once before marking the subtask terminal.
        for _ in range(2):
            if pick is None:
                break
            tried.append(pick.worker_id)
            started = self._now_ms()
            try:
                envelope = SubtaskDispatch(
                    subtask_id=subtask.id,
                    task_id=live.task_id,
                    specialty=specialty,
                    prompt=subtask.prompt,
                    source_inputs=[SourceInput(id=k, text=v) for k, v in resolved_inputs.items()],
                    deadline_ms=deadline_ms,
                )
                if self._token_issuer is not None:
                    token = self._token_issuer.issue_for(subtask=subtask, dispatch=envelope)
                    envelope = SubtaskDispatch(
                        subtask_id=envelope.subtask_id,
                        task_id=envelope.task_id,
                        specialty=envelope.specialty,
                        prompt=envelope.prompt,
                        source_inputs=envelope.source_inputs,
                        deadline_ms=envelope.deadline_ms,
                        capability_token=token.to_dict(),
                    )
                result = await self._dispatch_client.dispatch(
                    envelope,
                    worker_id=pick.worker_id,
                    deadline_ms=deadline_ms,
                )
            except SubtaskTimeoutError as exc:
                last_error = str(exc)
                last_outcome = SubtaskState.TIMED_OUT
                pick = self._scheduler.pick_for_retry(sched_st, registry, exclude_worker_ids=tried)
                continue

            if result.status == "COMPLETED":
                self._store.record(
                    Episode(
                        task_id=live.task_id,
                        subtask_id=subtask.id,
                        worker_id=pick.worker_id,
                        specialty=specialty,
                        model_version=result.model,
                        adapter_version="",
                        input_text=subtask.prompt,
                        trajectory=tuple(resolved_inputs.values()),
                        output_text=result.output,
                        success=True,
                        latency_ms=result.latency_ms or (self._now_ms() - started),
                        tokens_used=result.tokens_used,
                        outcome=SubtaskState.COMPLETED,
                        critic_score=0.0,
                        recorded_at_ms=self._now_ms(),
                    )
                )
                return result.output

            if result.status in _TERMINAL_FAILURE_STATUSES:
                last_error = result.error or result.status
                last_outcome = (
                    SubtaskState.TIMED_OUT if result.status == "TIMED_OUT" else SubtaskState.FAILED
                )
                pick = self._scheduler.pick_for_retry(sched_st, registry, exclude_worker_ids=tried)
                continue

            # NEEDS_SUBTASK from a remote worker is not wired in this slice —
            # treat as terminal rather than silently dropping it.
            last_error = f"unhandled remote status {result.status!r}"
            last_outcome = SubtaskState.FAILED
            break

        now = self._now_ms()
        self._store.record(
            Episode(
                task_id=live.task_id,
                subtask_id=subtask.id,
                worker_id=tried[-1] if tried else "<none>",
                specialty=specialty,
                model_version="",
                adapter_version="",
                input_text=subtask.prompt,
                trajectory=(),
                output_text=last_error,
                success=False,
                latency_ms=0,
                tokens_used=0,
                outcome=last_outcome,
                critic_score=0.0,
                recorded_at_ms=now,
            )
        )
        raise RuntimeError(f"remote dispatch for {subtask.id} exhausted retries: {last_error}")


_NEEDS_SUBTASK_DEFERRED = object()
_TERMINAL_FAILURE_STATUSES = frozenset({"FAILED", "TIMED_OUT", "REJECTED"})
