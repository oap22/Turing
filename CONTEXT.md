# Turing — Project Context

Domain language and load-bearing decisions for the Turing multi-edge research cluster. Read this before working in the repo; it's the shared vocabulary every ADR, issue, and module assumes.

## What Turing is

A personal AI cluster that runs domain research overnight and grows an Obsidian vault into a smarter knowledge base over time. Heterogeneous edge hardware (Pis, Jetson Orin Nanos, MacBook Pro) plus pull-only access to H100/DGX training nodes. Local-first by design; cloud (Claude API) reserved for hard tiers under a hard $10/day budget.

The current `main` is a single-agent Discord bot with mesh hooks. The PRD (#1) reshapes it into a coordinator + worker cluster with episodes, critic, and a self-improvement pipeline. ADRs in `docs/adr/` lock the boundaries before code moves.

## Topology

- **Coordinator** — single always-on node (Pi 5). Owns: task lifecycle, scheduler, capability registry, planner, budget gate, vault index, Discord bot, episode store, critic queue dispatch, adapter registry.
- **Worker** — many. Owns: executor (the agent loop), capability advertiser, tool router, low-risk tool execution. Workers never run Discord, never hold the shell safety gate.
- **Training facility** — H100/DGX. Pull-only via `turing-trainer` systemd unit subscribed to coordinator NATS. No inbound dial. Never joins runtime mesh.
- **Node assignments** — Pi 5 = coordinator; MBP = premium worker (also hosts critic 13B+ and MLX SFT trainer); Jetson Orin Nanos + Pi 4s = workers; **Pi 3 and Surface Pro excluded.**

## Runtime bus

- **NATS JetStream** is the runtime bus for all task / result / event traffic. LAN-only, TLS + nkey auth. Config admits a public flip later.
- **Pyre/Zyre is discovery-only** — announces the coordinator's NATS URL on the LAN. No task or result traffic ever rides Zyre. The legacy `mesh/` module collapses into `transport/` as a discovery shim.
- Every `MeshMessage` is Ed25519-signed by sender, verified by receiver. Replay protection via `request_id` dedup window.

## Module layout (post-refactor)

```
transport/          NATS client wrapper, MessageSigner, Pyre discovery shim
coordinator/        lifecycle, scheduler, registry, planner, budget, vault index, Discord bot
worker/             executor, capability advertiser, tool router
learning/critic/    async critic queue, Haiku calibration, drift detector
learning/trainer/   MLX SFT (MBP), DPO entry points, dataset builder
learning/patterns.py  reused as Phase A exemplar miner (story 66) — do not rewrite
vault/              vault index, watcher, vault_query / vault_propose tools
evals/<specialty>/  version-controlled per-specialty eval sets (Phase B β-eval gate)
deploy/             systemd units (turing-trainer), compose, deployment glue
```

### Legacy reuse vs retire

- `agent/core.py` — **reuse** as the worker executor (Perceive → Think → Act → Remember loop maps cleanly onto a worker handling one subtask).
- `mesh/` (Pyre/Zyre) — **collapse to discovery-only** under `transport/`. Drop the message-bus role.
- `learning/patterns.py` — **reuse** as the Phase A exemplar miner. Story 66 explicitly calls this out; do not rewrite.

## Core loop (per subtask)

A worker pulls a subtask off NATS → executor runs Perceive → Think → Act → Remember → returns `TASK_RESULT` (or `needs_subtask` to extend the DAG). Every closed subtask becomes an **episode** row: `task_id, worker_id, specialty, model+adapter version, input, trajectory, output, success, latency, tokens, outcome`. Episodes are the training corpus.

Every closed non-`REJECTED` episode is dispatched to the `judge`-specialty worker (MBP) for **critic scoring**; the score is written back to the episode row. Nightly at 02:00, top-K-by-score episodes per specialty are condensed into **lessons** — short, structured, vector-indexed, specialty-tagged. Workers retrieve top-3 lessons by `(specialty, prompt)` similarity and the executor renders them as a `<lessons>` block in the user message. Lessons TTL out at 60 days unless **pinned** by the prompt evolver citing them in a winning A/B prompt; pin renews on re-citation, lapses otherwise.

## Lifecycle

`PENDING → DISPATCHED → RUNNING → {COMPLETED, FAILED, TIMED_OUT, REJECTED, NEEDS_SUBTASK}`. Every transition appended to JetStream subject `tasks.<id>.events` with a SQLite projection. Subtask execution idempotent on `subtask_id`.

## Self-improvement phases

- **Phase A** — nightly prompt + few-shot exemplar evolution. Zero training compute. Reuses `learning/patterns.py`.
- **Phase B** — MBP MLX LoRA SFT. First target: `research-summarize`. Eval-gated promotion.
- **Phase C** — H100 DPO via `turing-trainer`. Same eval gate.

**Promotion gate.** Offline ≥2pp held-out improvement → `STAGED` in `AdapterRegistry`. K=1 round-robin canary worker re-runs the held-out eval at quantization; pass = `score ≥ prior_live_canary_score − 0.5pp` → fleet rollout to LIVE. Fail → permanent `REJECTED`, canary reverts, worst-failed cases written to `hard_examples` for next-cycle training, Discord notify with per-specialty regression rate.

## Safety boundaries

- **Shell runs on the coordinator only.** Workers emit signed `tool_request`; coordinator verifies a capability token (`subtask_id`-scoped: `allowed_commands_regex, cwd, timeout, expires_at`) before executing. One audit log, one gate.
- Workers execute low-risk tools locally: `vault_query`, `workspace_io`, `web_fetch` (allowlist), specialty tools.
- Adapters verified by SHA256 + Ed25519 signature on load. Mismatched-base or tampered adapters refused.
- Cross-DAG outputs wrapped in `<untrusted_data>` blocks to mitigate indirect prompt injection.
- **$10/day cloud budget**, hard cap. `BudgetGate` pre-call estimate, refusal at cap, midnight reset, fallback signal. Every Discord task reply shows remaining budget.

## Vault

Coordinator hosts a vector index of the operator's Obsidian vault, refreshed by a filesystem watcher on every commit. Workers ground via `vault_query(query, k)`. Workers propose new notes via `vault_propose` → `vault/inbox/<task_id>/<slug>.md` with frontmatter (`source, task_id, specialty, confidence, critic_score`). Curated vs inbox is purely path-based — `vault/inbox/**` is the only writeable path.

## Surfaces

- **Discord** — only user-facing surface initially. One live-editing message per task with per-subtask thumbs-up/down threads. Reward attribution: subtask thumb → that episode (±1.0); main thumb → synthesis episode (±1.0) + fractional credit (±0.3) to subtasks whose `output_key ∈ synthesis.consumed_keys`; no feedback in 24h → `critic_fallback` reward in `[-0.3, +0.3]`. Rewards are *events* in `episode_rewards(episode_id, source, value, recorded_at)`; effective reward is `SUM(value)` per episode. Bot un-reactions during outages reconcile via cancellation rows on reconnect.
- **CLI / API** — out of scope for Phase 0.

## Issue tracker

GitHub Issues at `oap22/Turing`, managed via `gh`. Triage labels: `needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix`. ADRs are `ready-for-human` because they need operator decisions; slices are `ready-for-agent`.
