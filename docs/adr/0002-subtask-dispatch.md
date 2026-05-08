# ADR 0002: Subtask dispatch over NATS

- **Status:** Proposed
- **Date:** 2026-05-08
- **Slice:** #90 — follow-up to #86
- **Covers user stories:** the worker-pool runtime that ADR 0001's DAG dispatches into

## Context

ADR 0001 locks the DAG schema. The orchestrator currently runs subtasks
in-process — there are no `bus.publish()` calls for the worker pool. To
graduate to a multi-node cluster (and to power the eval harness's
`coordinator_worker`, AC #2 of #86), we need a wire contract that lets the
coordinator dispatch a subtask to a remote worker, await a result, and
handle the failure modes JetStream gives us.

The worker side is largely empty today — `worker/executor/` and
`worker/advertiser/` are package skeletons. So this ADR locks the contract
that those modules will be built against, not the implementation itself.

The pieces already in place we want to keep:

- `transport/nats_bus.py` — TLS + nkey, LAN-only enforcement
- `transport/envelope.MeshMessage` — Ed25519-signed envelope with
  `request_id, sender_id, subject, payload, timestamp_ms`
- `transport/envelope.ReplayWindow` — request-id dedup
- `coordinator/scheduler/scheduler.py` — `pick(subtask, registry) → manifest`
  using `eval_score` ranking
- `coordinator/registry/` — capability manifests advertised by workers

Pieces missing that this ADR specifies:

- NATS subject naming for subtask traffic
- The `SubtaskDispatch` and `TaskResult` payload schemas
- The reply-correlation pattern for one-shot request/response over a
  durable stream
- How a single eval-harness caller (or the orchestrator) targets a
  specific worker vs. load-balances across the pool

## Decision

### Messaging pattern: JetStream queue group + ephemeral reply subject

Subtasks ride a **JetStream durable stream** (at-least-once, redelivery on
worker crash). Workers consume via a **queue group** so each subtask is
delivered to exactly one worker at a time. Results ride a **plain core NATS
subject** the coordinator subscribes to *before* publishing.

- Durable streams matter for dispatch — losing a subtask because a worker
  crashed before ack would silently drop work.
- Plain subjects suffice for results because the coordinator is alive and
  listening; a coordinator restart is a separate fault we handle by
  re-dispatching from the lifecycle log (`tasks.<id>.events`).

We considered plain `nc.request()`: simpler, built-in correlation, but
fire-and-forget loses subtasks on worker failure mid-execution. Rejected.

### Subject naming

Two dispatch subjects per specialty:

```
subtasks.<specialty>                     # JetStream durable, queue group
subtasks.workers.<worker_id>             # plain NATS, point-to-point
subtasks.<subtask_id>.result             # plain NATS, ephemeral reply
```

Workers subscribe to **both** dispatch subjects:

- The specialty queue handles normal load-balanced traffic. NATS routes one
  copy per subtask to one worker in the group.
- The worker-direct subject handles eval/promotion testing where the
  coordinator needs to target a specific worker (e.g. the one carrying a
  candidate adapter under Phase B β-eval).

Specialty values come from `coordinator/planner/schema.Specialty`:
`research-discover`, `research-deep`, `research-summarize`, `synthesis`,
`judge`, `planner`.

### Reply correlation

The coordinator subscribes to `subtasks.<subtask_id>.result` **before**
publishing the dispatch. The dispatch envelope carries the same
`subtask_id` so the worker knows which reply subject to publish to. This
sidesteps NATS's built-in `reply` slot (which is awkward through
JetStream) and keeps subjects deterministic and easy to inspect with
`nats sub`.

### Envelope: `SubtaskDispatch`

Wrapped in a signed `MeshMessage` (subject = `subtasks.<specialty>` or
`subtasks.workers.<id>`):

```json
{
  "version": 1,
  "subtask_id": "st_a1b2c3",
  "task_id": "t_xyz",
  "specialty": "research-summarize",
  "prompt": "Summarize the discoveries written to ...",
  "source_inputs": [
    {"id": "src_1", "url": "...", "text": "..."}
  ],
  "deadline_ms": 1715200000000,
  "capability_token": null
}
```

- `version` — schema version; bumped on breaking changes.
- `subtask_id` — globally unique. The coordinator generates it, and it is
  the dedup key for at-least-once retries (CONTEXT.md: "Subtask execution
  idempotent on `subtask_id`.").
- `task_id` — parent task, lets workers thread context retrieval.
- `specialty` — required for dispatch routing and worker self-checks
  (worker rejects if specialty ∉ manifest.specialties).
- `prompt` — opaque to dispatch infra; meaningful only to the worker's
  specialty pipeline.
- `source_inputs` — `SourceDoc[]` matching the eval-harness shape and
  ADR 0001's untrusted-data wrapper convention. Worker is responsible for
  wrapping in `<untrusted_data>` blocks before injecting into its LLM.
- `deadline_ms` — wall-clock UNIX milliseconds at which the result is
  considered late. Worker should self-cancel by then. Coordinator's own
  timer is the safety net.
- `capability_token` — optional in v1. Slot reserved for the
  `subtask_id`-scoped token CONTEXT.md describes (allowed_commands_regex,
  cwd, timeout, expires_at) when shell-tool work lands. Workers without a
  token cannot make `tool_request` calls back to the coordinator.

### Envelope: `TaskResult`

Published to `subtasks.<subtask_id>.result`, also signed:

```json
{
  "version": 1,
  "subtask_id": "st_a1b2c3",
  "worker_id": "mbp-premium-1",
  "status": "COMPLETED",
  "output": "...",
  "tokens_used": 1432,
  "latency_ms": 4120,
  "model": "research-summarize-v3-lora",
  "error": null
}
```

- `status` ∈ {`COMPLETED`, `FAILED`, `TIMED_OUT`, `REJECTED`,
  `NEEDS_SUBTASK`} — same set ADR 0001 locks for the lifecycle.
- `output` — the worker's primary text output (the summary, etc.). Empty
  string for non-COMPLETED.
- `error` — populated for FAILED / REJECTED with a short reason; null
  otherwise.
- `NEEDS_SUBTASK` results carry an additional `fragment` field per ADR
  0001 §4.

### Signing & replay

Every dispatch and result is wrapped in `MeshMessage` and signed with the
sender's Ed25519 key. Receivers verify against the public key advertised
in the sender's `CapabilityManifest` (workers) or a known coordinator
public key (results). The existing `transport.signer` and
`transport.signed_transport` modules apply unchanged.

The replay window TTL applies to dispatch (60s default — well under the
typical subtask deadline). Result subjects are subtask-scoped and
ephemeral so dedup is enforced by `subtask_id` rather than the replay
window.

### Worker selection split

- **Orchestrator → specialty queue** for normal traffic. The Scheduler's
  `pick()` becomes informational/observability-only on this path; the
  queue group does the load balancing.
- **Eval / promotion → worker-direct.** The eval harness's
  `coordinator_worker(coordinator_url, worker_id=...)` publishes to
  `subtasks.workers.<worker_id>` to gate Phase B promotion against a
  specific candidate adapter.

This preserves the Scheduler's eval-score ranking for cases where the
coordinator wants to bypass the queue (e.g., scheduling a known-best
worker for high-stakes subtasks, or driving a beta worker only when its
manifest is loaded).

### Timeouts: defense in depth

- Coordinator publishes with `deadline_ms`.
- Worker self-cancels after `deadline_ms` (returns `TIMED_OUT`).
- Coordinator's own timer fires `deadline_ms + 30s` as backup; if no
  result received, the subtask transitions to `TIMED_OUT` in the
  lifecycle log.
- A late `TaskResult` arriving after coordinator timeout is logged but
  ignored (idempotency on `subtask_id`).

### Retry & idempotency

JetStream redelivers on worker ack failure (crash mid-execution). Workers
MUST be idempotent on `subtask_id` — episode store rejects duplicate
inserts (CONTEXT.md). On second delivery, a worker that has already
emitted a `TaskResult` for the same `subtask_id` re-emits a cached
`COMPLETED` rather than re-running the LLM.

The `Scheduler.pick_for_retry(...exclude_worker_ids)` method already
exists for the case where the coordinator decides to retry on a different
worker after `FAILED`/`TIMED_OUT`.

## Out of scope

- **Capability-token issuance & shell-tool gating.** v1 envelope reserves
  the field; the issuance/verification module lands in a follow-up.
- **JetStream stream provisioning** (retention, max-bytes, replicas).
  Operational concern; not part of the wire contract.
- **Worker self-registration / heartbeat protocol.** Tracked separately;
  workers must be in the registry before dispatch routes to them.

## Implementation slices

Each slice is independently agent-runnable. File as separate issues
labelled `ready-for-agent`:

1. **SubtaskDispatchClient** — `coordinator/dispatch/` module that
   publishes a signed `SubtaskDispatch`, subscribes to the result
   subject, returns the `TaskResult` (or raises on timeout). Tests against
   `transport.bus.InMemoryBus`.
2. **Worker subscriber loop** — `worker/executor/` subscribes to its
   specialty queue and worker-direct subject, runs the executor pipeline,
   publishes `TaskResult`. Idempotent on `subtask_id`.
3. **Scheduler integration** — orchestrator publishes via
   `SubtaskDispatchClient` instead of running subtasks in-process when
   the registry shows the picked worker is on a remote node.
4. **Eval harness `coordinator_worker`** — thin wrapper around
   `SubtaskDispatchClient` for the harness CLI's `--worker coordinator
   --url ... [--worker-id ...]`.
5. **Capability-token v1** — issuance + verification + worker-side gate
   for `tool_request` callbacks.

## Consequences

- The eval harness gets a `coordinator_worker` once slice 4 lands,
  unblocking Phase B promotion gating against real worker pools.
- The contract is **versioned** (`version: 1` in both envelopes) — additive
  fields don't bump the version; renames or removals do, with a manifest
  schema version bump on workers.
- Workers depend only on the wire contract, not on coordinator internals.
  A heterogeneous pool (Pi 4, Jetson, MBP) can run different worker
  implementations as long as they speak v1.
- The Scheduler's eval-score ranking is **preserved but de-emphasised** for
  the normal-traffic path. If empirical use shows queue-group balancing
  wastes premium-worker capacity, we revisit by splitting per-tier
  queues (`subtasks.<specialty>.premium`, `.standard`).
