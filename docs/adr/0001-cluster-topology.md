# ADR 0001 — Cluster topology and module layout

- Status: Accepted
- Date: 2026-05-07
- Closes: #3

## Context

Current `main` is a single-agent Discord bot with mesh hooks (`agent/`, `mesh/`, `learning/patterns.py`, `tools/`, `memory/`). The PRD (#1) reshapes Turing into a coordinator + worker cluster with NATS JetStream as the runtime bus, episodes as the training corpus, and a self-improvement pipeline (prompt evolution → MLX SFT → DPO). Before any code moves we lock module boundaries, node roles, and which legacy modules survive — otherwise every downstream slice (#4–#28) drifts.

## Decision

### Node roles

- **Coordinator** — Pi 5. Single, always-on. Owns lifecycle, scheduler, capability registry, planner, budget gate, vault index, Discord bot, episode store, critic queue dispatch, adapter registry. **Discord lives only here.**
- **Premium worker** — MacBook Pro. Also hosts the local 13B+ critic and the MLX SFT trainer (Phase B).
- **Workers** — Jetson Orin Nanos and Pi 4s. Pull subtasks off NATS, advertise capability manifests, run low-risk tools locally.
- **Training facility** — H100/DGX. Pull-only via `turing-trainer` systemd unit. No inbound connection. Never joins runtime mesh, never holds a worker key.
- **Excluded** — Pi 3 (too weak), Surface Pro (not always-on, Windows host friction).

### Coordinator vs worker boundary

| Concern | Coordinator | Worker |
| --- | --- | --- |
| Discord ingress | ✅ | — |
| Task lifecycle / event log | ✅ | — |
| Planner (LLM DAG) | ✅ | — |
| Capability registry | ✅ (authoritative) | advertises |
| Scheduler / dispatch | ✅ | — |
| Budget gate (cloud spend) | ✅ | — |
| Vault index + watcher | ✅ | queries via tool |
| Episode store + critic queue | ✅ | — |
| Adapter registry / verification | ✅ (issuer) | verifies on load |
| Shell execution | ✅ (sole gate, audit log) | emits signed `tool_request` |
| Low-risk tools (`vault_query`, `workspace_io`, `web_fetch` allowlist) | — | ✅ |
| Executor (Perceive→Think→Act→Remember) | — | ✅ |

### Runtime bus

- **NATS JetStream** is the runtime bus for all task / result / event traffic. LAN-only, TLS + nkey auth from day one. Public flip is a config change, not a refactor.
- **Pyre/Zyre is discovery-only.** It announces the coordinator's NATS URL on the LAN; no task / result / event traffic rides Zyre. The current `mesh/` module collapses into `transport/` as a thin discovery shim.

### Module layout

```
transport/          NATS client wrapper, MessageSigner (Ed25519), Pyre discovery shim
coordinator/
  lifecycle/        TaskLifecycle state machine, episode store
  scheduler/        dispatch, retry policy
  registry/         CapabilityRegistry
  planner/          LLM DAG planner, schema (see ADR 0002)
  budget/           BudgetGate, cloud LLM gateway
  vault_index/      vector index + filesystem watcher
  discord_bot/      live-DAG renderer, thumbs reward attribution
  adapters/         AdapterRegistry, manifest verification
worker/
  executor/         the Perceive→Think→Act→Remember loop
  advertiser/       capability manifest publish + heartbeat
  tools/            local tool router; signed-token shell client
learning/
  critic/           async critic queue, Haiku calibration, drift detector
  trainer/          MLX SFT (MBP), DPO dataset builders
  patterns.py       reused as Phase A exemplar miner (do not rewrite)
vault/              shared types, frontmatter validators
evals/<specialty>/  version-controlled eval sets, Phase B β-eval
deploy/             systemd units (turing-trainer), compose, deployment glue
```

### Legacy reuse vs retire

- `agent/core.py` — **reuse** as the worker executor. The existing Perceive→Think→Act→Remember loop is exactly what a worker does for one subtask. Wire it under `worker/executor/`.
- `agent/safety.py` — **reuse** worker-side as defense-in-depth alongside the coordinator's capability-token gate.
- `mesh/` — **retire its message-bus role.** Keep only the Pyre announce/listen path, move it under `transport/`.
- `learning/patterns.py` — **reuse** as the Phase A exemplar miner. Story 66 explicitly mandates this; do not rewrite.
- `tools/`, `memory/`, `llm/`, `discord_bot/`, `plugins/` — split: tool registry + safety gate move to coordinator; memory/episode persistence moves to coordinator (`coordinator/lifecycle/`); LLM router stays cross-cutting under `llm/`; Discord moves to `coordinator/discord_bot/`; plugin loader stays cross-cutting.

### Skeleton

Acceptance for #3 says create the package skeleton with `__init__.py` placeholders so subsequent slices have a home. We will create:

- `src/turing/transport/__init__.py`
- `src/turing/coordinator/__init__.py` (+ subpackages above)
- `src/turing/worker/__init__.py` (+ subpackages above)
- `src/turing/learning/critic/__init__.py`
- `src/turing/learning/trainer/__init__.py`
- `src/turing/vault/__init__.py`
- `evals/.gitkeep`
- `deploy/.gitkeep`

No code moves in this ADR — Slice 2 (#4) is the first to actually populate `transport/`.

## Consequences

- Every downstream slice (#4–#28) gets a stable home for its module.
- The coordinator is the single point of: shell execution, cloud spend, Discord, lifecycle truth. That's intentional — one safety gate, one audit log, one budget.
- The MBP carries a lot of weight (premium worker + critic + MLX trainer). If it's offline, Phase B training pauses and critic backlog grows; the cluster still serves tasks via Pi 4s/Jetsons.
- Pi 3 / Surface Pro exclusion means we never spend slices on their quirks.
- `mesh/` losing its message-bus role is a breaking change — anything currently routing through it must move to `transport/` in Slice 2.

## Alternatives considered

- **Keep mesh as the bus, skip NATS.** Rejected: Pyre/Zyre has no durable subjects, no object store, no replay — we'd reinvent JetStream poorly.
- **Symmetric peers (no coordinator).** Rejected: shell execution and cloud spend need a single gate; Discord needs a single owner. Symmetry costs more than it buys at this scale.
- **Discord on every worker.** Rejected: multiple bots fighting for the same channel; cannot enforce one budget gate.

## References

- PRD #1 (parent)
- User stories 26, 56–66, 79, 80
- ADR 0002 (DAG schema, planner contract) — closes #8
