# PRD: Multi-edge orchestration framework — Turing as a research cluster

> Draft to publish as a GitHub issue with label `ready-for-agent`.
> To publish: `gh issue create --title "PRD: Multi-edge orchestration framework — Turing as a research cluster" --label ready-for-agent --body-file .scratch/prd-multi-edge-cluster.md`
> (omit the heading lines above when copy-pasting, or use `--body-file` directly — gh will accept the file as-is)

## Problem Statement

I want a personal AI cluster that does domain research for me overnight and grows my Obsidian vault into a smarter knowledge base over time. I have heterogeneous edge hardware (Pis, Jetson Orin Nanos, MacBook Pro, Surface Pro) plus training-only access to H100/DGX nodes, and I want them to work together as a coordinated cluster — a master node directing specialty workers, with workers that can fail, learn from those failures, and improve themselves through fine-tuning. The current Turing repo is a single-agent Discord bot with mesh hooks; it does not coordinate work across nodes, has no notion of specialties, and has no self-improvement loop. I'm a student on a tight budget (≤$10/day cloud spend) so the design must be local-first with cloud reserved for hard tiers.

## Solution

Reshape the repo around a single always-on **coordinator** (Pi 5) that orchestrates a pool of LLM-capable workers via NATS JetStream. Workers advertise capability manifests (specialty, base model, adapters, tools, hardware); the coordinator runs an LLM planner that decomposes user tasks into a DAG of subtasks and dispatches each to the best-matched worker. Every subtask emits a structured episode (input, trajectory, output, latency, tokens) that an async critic scores; episodes are the training corpus for an offline self-improvement pipeline (prompt evolution → LoRA SFT on the MBP via MLX → DPO on the H100). Workers grow an Obsidian vault by writing proposed notes to `vault/inbox/` for human triage; they auto-research overnight against a `goals.yaml` of arxiv/HN/RSS goals. Discord is the only user-facing surface initially — a live-editing DAG message with per-subtask thumbs-up/down threads — and a hard $10/day budget gate enforces graceful local-only fallback when exhausted.

## User Stories

1. As the operator, I want to send a research task to a Discord channel, so that the cluster can decompose and execute it across multiple specialty workers.
2. As the operator, I want to see a live-updating DAG message in Discord while a task runs, so that I can watch which subtasks are dispatched, running, completed, or failed in real time.
3. As the operator, I want each subtask line in the DAG message to show the worker name and adapter version, so that I can attribute outcomes to specific specialty configurations.
4. As the operator, I want to react with a thumbs-up/down on a subtask thread, so that I can give per-subtask reward signals that train each specialty independently.
5. As the operator, I want thumbs-up/down on the final synthesis message to attribute reward to the synthesis step plus a fractional credit to subtasks whose workspace outputs were actually consumed, so that strong reward signal lands on the right episodes.
6. As the operator, I want the cluster to fall back to local-only mode when the daily cloud budget is exhausted, so that I never get a surprise bill.
7. As the operator, I want the remaining daily budget shown in every Discord task reply, so that I can see how much cloud spend a task burned.
8. As a worker node, I want to advertise a capability manifest at registration (specialties, base model, adapters, tools, hardware, max_concurrent, eval score), so that the coordinator can route subtasks correctly.
9. As a worker node, I want to refuse subtasks whose required tools aren't a subset of my specialty manifest, so that a research worker can never be asked to run shell.
10. As the coordinator, I want to maintain a worker registry that updates on heartbeat and capability re-announcement, so that scheduling decisions reflect live state.
11. As the coordinator, I want to dispatch subtasks only to workers where `in_flight < max_concurrent` AND specialty matches AND requested tools are within the manifest, breaking ties by recent eval score.
12. As the coordinator, I want to retry a failed or timed-out subtask on a different worker of the same specialty when one is available, falling back to terminal otherwise, so that transient failures don't kill multi-hour tasks.
13. As the coordinator, I want failed-then-succeeded retries to record both episodes (the failure and the success), so that failure trajectories feed negative training signal.
14. As the coordinator, I want every state transition appended to a durable JetStream subject `tasks.<task_id>.events` with a SQLite projection, so that I can recover the full DAG after a restart and resume in-flight work.
15. As the coordinator, I want subtask execution to be idempotent on `subtask_id`, so that reissues after my crash don't double-execute.
16. As the coordinator, I want an LLM planner that emits a typed DAG `[{id, specialty_required, prompt, depends_on, required_tools}]` for non-trivial tasks, so that decomposition is structured and debuggable.
17. As the coordinator, I want trivial single-specialty tasks to bypass the planner via a cheap classifier, so that simple work doesn't pay planning cost.
18. As a worker, I want to return `status: needs_subtask` with a sub-prompt + required specialty when I encounter work outside my domain, so that the coordinator can extend the DAG without me sub-delegating directly.
19. As a worker, I want to read inputs from and write outputs to a per-task workspace addressed by reference (`workspace://task-abc/key`), so that large blobs don't ride the message bus and downstream subtasks can fetch lazily.
20. As the coordinator, I want each task's workspace to be GC'd 24h after task completion (with relevant entries archived to the episode store), so that the broker doesn't grow unbounded.
21. As a worker, I want to query a master-served vault index via a `vault_query(query, k)` tool, so that I can ground my outputs in the operator's curated knowledge.
22. As a worker, I want to propose new notes via `vault_propose` that write to `vault/inbox/<task_id>/<slug>.md` with frontmatter (source, task_id, specialty, confidence, critic_score), so that auto-research outputs flow into a human-reviewable funnel without polluting curated notes.
23. As the operator, I want the vault repo's git history to record what the cluster contributed and what I accepted, so that I have an audit trail of cluster-derived knowledge.
24. As the coordinator, I want to maintain a vector index of the vault on every commit (filesystem watcher), so that worker queries reflect the current vault state.
25. As the coordinator, I want each task end to extract a short structured "lesson" per specialty (what worked, what didn't) and index it in a vector store, so that future subtasks can retrieve top-k relevant lessons and inject them into worker prompts.
26. As the coordinator, I want every `TASK_REQUEST` and `TASK_RESULT` to be Ed25519-signed by sender and verified by receiver, so that a stolen worker key cannot impersonate the coordinator.
27. As a worker, I want to refuse `shell` tool calls unless they carry a coordinator-signed capability token declaring `allowed_commands_regex`, `cwd`, `timeout`, `expires_at`, scoped to my `subtask_id`, so that even a buggy coordinator cannot make me run arbitrary shell.
28. As the coordinator, I want all `shell` tool execution to happen on the coordinator (master-side), with workers emitting signed `tool_request` messages, so that there is one shell safety gate and one audit log for the entire cluster.
29. As a worker, I want to execute low-risk tools locally (`vault_query`, `workspace_io`, `web_fetch` with allowlist, specialty-specific tools), so that the broker isn't a chokepoint for cheap calls.
30. As the coordinator, I want adapters distributed via NATS object store with a signed manifest containing SHA256 + base_model + eval_score, so that workers can verify integrity before loading.
31. As a worker, I want to verify an adapter's hash and signature before loading it, so that a tampered object store cannot poison my model.
32. As a worker, I want output that flows into another worker's prompt to be wrapped in `<untrusted_data>` blocks with a system instruction to treat contents as data, so that indirect prompt injection across the DAG is mitigated.
33. As the coordinator, I want to write every closed subtask as an episode row (`task_id, worker_id, specialty, model+adapter version, input, trajectory, output, success, latency, tokens, outcome`), so that the corpus is SFT/DPO-ready from day one.
34. As the coordinator, I want an async critic to score every episode on `correctness`, `efficiency`, `specialty_fit` 0–1 with a free-text critique, attached to the episode row, so that I have RL-shaped data without waiting for user feedback.
35. As the coordinator, I want the critic to run as a local 13B+ model on the MBP with ~5% of episodes additionally scored by Claude Haiku for calibration, so that I get cheap quality data with drift detection.
36. As the coordinator, I want to recalibrate the local critic against Claude scores when drift exceeds threshold, so that critic quality doesn't silently degrade.
37. As the coordinator, I want a nightly job that for each specialty pulls top-K episodes by critic score, regenerates the system prompt + few-shot exemplars, A/B routes 10% of new tasks to the new prompt, and promotes if win-rate beats threshold, so that the cluster improves with zero training compute (Phase A).
38. As the operator, I want a per-specialty eval set (`evals/<specialty>/*.jsonl`) version-controlled in the repo with `{prompt, expected_*, scoring_fn}`, so that adapter promotion is gated on held-out performance (Phase B β-eval).
39. As a worker, I want to run my current eval at registration and on adapter swap, so that the coordinator knows my live capability score, not just the manifest claim.
40. As the coordinator, I want training jobs proposed to the operator via Discord with an approve/reject button, so that early autonomy is bounded by manual confirmation.
41. As the coordinator, I want training-job autonomy to graduate as trust builds (manual → manual-with-defaults → auto-within-quota), so that I'm not stuck approving every nightly job forever.
42. As a training-facility node (H100/DGX), I want to run a `turing-trainer` systemd service that subscribes to `training.queue.<facility>` on the coordinator's NATS, pulls dataset bundles, runs LoRA SFT/DPO with torch+peft (or unsloth), and publishes signed adapters to the object store, so that training runs are pull-based and don't require inbound connections to the facility.
43. As a training-facility node, I want training logs streamed back to the coordinator as `training.events.<job_id>`, so that I can watch progress in Discord/dashboard.
44. As the coordinator, I want LoRA SFT for 7B-class models to also be runnable on the MBP via MLX, so that I have a fallback when the H100 is unavailable and a fast iteration path for small jobs.
45. As the coordinator, I want a promotion gate that verifies adapter signature, SHA256, and eval improvement of ≥2% on held-out before tagging an adapter as `staged`, so that bad fine-tunes don't hit production.
46. As the coordinator, I want staged adapters rolled out to one worker first, with that worker re-running eval at its actual quantization, then to all matching workers if the live eval confirms, so that quantization regressions are caught before fleet-wide rollout.
47. As the coordinator, I want fleet rollout halted and the operator notified in Discord if any worker shows live-eval regression, so that I never silently degrade a specialty.
48. As the coordinator, I want failed training runs (didn't promote) to write a "hard examples" batch to the episode store and post a summary to Discord, so that failures are themselves data.
49. As the coordinator, I want a `goals.yaml` registry of standing per-specialty goals with cadence and required tools, so that idle workers can run auto-research overnight.
50. As the coordinator, I want auto-research runs only when (a) cluster has been idle ≥N minutes, (b) wall-clock is in 02:00–06:00 local, (c) the specialty's monthly research budget hasn't been exhausted, so that auto-research never interferes with user work or runs away on cost.
51. As the coordinator, I want auto-research to use local models exclusively initially (zero cloud spend), so that γ runaway loops cannot bill cloud dollars.
52. As the operator, I want auto-research outputs deposited in `vault/inbox/auto_research/<specialty>/<date>/`, so that I can triage them in batches.
53. As the operator, I want a budget gate in front of every cloud-LLM call that estimates cost from token counts, refuses calls that would exceed remaining daily budget, and falls back to a local model, so that the cluster gracefully degrades instead of failing.
54. As the operator, I want the daily budget to reset at local midnight, so that the gate is predictable.
55. As the coordinator, I want to track per-task cloud cost attribution, so that I can answer "which tasks burned my budget."
56. As the operator, I want the Pi 5 to act as the always-on coordinator running NATS, the task lifecycle, the episode store, the worker registry, the scheduler, the Discord bot, and the vault index, so that the cluster's control plane never depends on a laptop being open.
57. As the operator, I want the MBP to register as a "premium worker" hosting the local critic, planner, synthesis specialty, and Phase-B trainer, so that high-end faculties are available when the laptop is on but the cluster degrades gracefully when it isn't.
58. As the operator, I want both Jetson Orin Nano dev kits hosting `research-deep` and `research-summarize` specialties with 7B q4 models on CUDA, so that the strongest sustained-throughput nodes do the heavy research work.
59. As the operator, I want both Pi 4s (8GB each) hosting `research-discover` (3B q4) with one as hot-spare or shard, so that polling/filtering is cheap and redundant.
60. As the operator, I want the Pi 3 explicitly excluded from the worker pool because 1GB RAM cannot run useful LLMs, so that the registry isn't polluted with non-functional nodes.
61. As the operator, I want NATS bound LAN-only initially with TLS + nkey auth configured from day one, so that flipping to public listening for a remote Jetson or rented H100 is a one-line config change.
62. As the operator, I want the cluster's Discord bot to live only on the coordinator (no worker runs Discord), so that user interactions are centralized.
63. As the operator, I want the `agent/core.py` perceive→think→act→remember loop split into a coordinator-side orchestration loop and a worker-side execution loop, so that responsibilities are clean.
64. As the operator, I want the existing single-node Turing bot's behavior subsumed into the coordinator + premium-worker pair, so that the working bits of the legacy code are reused rather than running in parallel.
65. As the operator, I want Pyre/Zyre kept only for initial peer discovery (announcing the coordinator's NATS URL) with all task/result/object traffic on NATS, so that I keep what works without forcing it to do what it's bad at.
66. As the operator, I want the existing `learning/patterns.py` reused as the implementation backing Phase A prompt evolution, so that prior work isn't thrown away.
67. As the operator, I want a kanban-style backlog view (columns per specialty, swimlanes per worker, cards per subtask state) eventually surfaced as a stripped-down dashboard, so that "scrum board for agents" is the natural mental model when curation pain warrants it.
68. As the operator, I want the dashboard deferred until I find myself querying `episodes.db` manually more than once a week, so that I don't sink time into ops UI before the cluster has produced anything worth curating.

## Implementation Decisions

**Topology and roles.**
- **Coordinator (Pi 5, 8GB):** sole always-on control-plane node. Runs NATS JetStream broker, task lifecycle state machine, SQLite episode store, worker registry, scheduler, Discord bot, vault filesystem watcher + index, budget gate, cloud-LLM gateway, signing key custody.
- **Premium worker (MBP M4 Pro, 48GB unified):** registers as a normal worker with capability manifest advertising specialties `judge`, `planner`, `synthesis`. Hosts the local 13B+ critic, multi-step DAG planner, synthesis specialist (13B–30B), and Phase-B trainer (MLX, 7B-class LoRA). Cluster degrades gracefully when offline.
- **Research-deep / research-summarize (Jetson Orin Nano, 8GB Ampere ×2):** 7B q4 with CUDA. Two boxes give same-specialty failover.
- **Research-discover (Pi 4 8GB ×2):** 3B q4, one active + one hot-spare or shard.
- **Pi 3, Surface Pro:** out of pool initially.
- **Training facility (H100 + DGX):** offline batch service, not on the mesh, talks to coordinator only via the `turing-trainer` pull agent.

**Transport.**
- NATS JetStream is the bus for everything except discovery. Subjects: `tasks.<task_id>.events` (durable, source of truth), `subtasks.<id>.dispatch`, `subtasks.<id>.result`, `tools.shell.requests`, `training.queue.<facility>`, `training.events.<job_id>`, `training.results.<job_id>`. KV buckets: `workers`, `adapters`, `goals`. Object store: workspace blobs, dataset bundles, adapter binaries.
- Pyre/Zyre kept solely for announcing the coordinator's NATS URL on the LAN.

**Memory tiers.**
| Tier | Scope | Storage | Writer | Reader |
|---|---|---|---|---|
| Per-node memory | One worker | Local SQLite | That worker | That worker only |
| Task workspace | One top-level task | NATS KV/Object (TTL = task end + 24h) | Any worker on that task | Any worker on that task |
| Lessons | Cross-task, per-specialty | Vector store on coordinator | Coordinator (extracted by critic) | All workers (auto-injected into prompts) |
| Obsidian vault | Cross-task, human-curated | Git repo on coordinator | Operator (curated); workers via `vault/inbox/` only | All workers via `vault_query` tool |

**Task lifecycle state machine.**
```
PENDING → DISPATCHED → RUNNING → {COMPLETED, FAILED, TIMED_OUT, REJECTED, NEEDS_SUBTASK}
                                          ↓
                                     RETRYABLE? → PENDING (attempt++, prefer different worker)
                                          ↓
                                     TERMINAL → critic pass → episode closed
```
Outcome tags drive training-data treatment: `COMPLETED` → critic-scored positive corpus; `FAILED`/`TIMED_OUT` → captured with truncated trajectory, included as negatives; `REJECTED` (safety gate) → captured for audit but **excluded from positive training data** to prevent teaching the model to evade safety; `NEEDS_SUBTASK` → not a failure, coordinator extends DAG.

**Capability manifest (worker advertisement).**
```yaml
worker_id: jetson-orin-1
specialties: [research-deep]
base_model: qwen2.5:7b-instruct-q4_k_m
adapters: [research-deep-lora-v3]
tools: [web_fetch, vault_query, workspace_io]
hardware: { device: jetson-orin-nano, vram_mb: 8000, ram_mb: 8000 }
max_concurrent: 1
eval_score: { research-deep: 0.74 }
public_key: <ed25519>
```

**Tool execution split.**
- On worker: `vault_query`, `workspace_io`, `web_fetch` (allowlist), `lsp`, specialty-specific tools.
- On coordinator: `shell` (always, for everyone), `vault_propose`, `web_fetch` (non-allowlist), `git_*`. Workers emit signed tool_request → coordinator verifies capability token → executes → returns result.

**Trust model.**
- NATS auth via nkey + TLS (LAN-only initially, design admits public flip).
- Application-layer Ed25519 signature on every `MeshMessage`. Coordinator signing key on Pi 5; each worker has its own keypair, registered.
- `shell` requests carry coordinator-signed capability tokens scoped to `subtask_id` with `allowed_commands_regex`, `cwd`, `timeout`, `expires_at`. Workers refuse without a valid token.
- Adapters: signed manifest with SHA256; workers verify before load.
- Cross-DAG output sanitization: untrusted blobs wrapped in `<untrusted_data>` with system-prompt instruction.

**Per-specialty starter roster.**
| Specialty | Host | Model | Tools |
|---|---|---|---|
| `research-discover` | Pi 4 (×1+1 spare) | 3B q4 | `web_fetch`(allowlist), `workspace_io`, `vault_query` |
| `research-deep` | Jetson Orin #1 | 7B q4 CUDA | + master-side `web_fetch` |
| `research-summarize` | Jetson Orin #2 | 7B q4 CUDA | + `vault_propose` |
| `synthesis` | MBP | 13B–30B; Claude fallback | `vault_query`, `workspace_io` |
| `judge` (critic) | MBP | 13B; Claude Haiku for ~5% calibration | `workspace_io` |
| `planner` | MBP for trivial; Claude Sonnet for complex | — | none |

**Self-improvement phases.**
- **Phase A (day 1):** Nightly per-specialty prompt + exemplar regeneration from top-K episodes. Zero training compute. A/B promotion.
- **Phase B (week 5–6):** LoRA SFT on MBP via MLX for 7B-class. Eval-gated promotion (≥2% improvement on held-out). K-worker live re-eval before fleet rollout.
- **Phase C (month 2+):** DPO on H100 via torch+peft. Same gate.
- **Phase D:** Manual H100/DGX RL, no ambient infra.
- First full Phase-B target: `research-summarize` (high signal density, easy to evaluate against vault voice).

**Auto-research (γ).**
- `goals.yaml` lists `[specialty, goal_text, cadence, max_cost_per_run, required_tools]`.
- Scheduler triggers when cluster idle ≥N minutes AND wall-clock ∈ [02:00, 06:00] local AND budget remaining.
- Local-only initially (zero cloud spend). Outputs land in `vault/inbox/auto_research/<specialty>/<date>/`.

**Budget gate.**
- Pre-call interceptor on every cloud-LLM call. Estimates cost from token counts × model price. If `remaining_today < estimated`, refuses and the caller routes to a local model. Resets at local midnight. Discord shows remaining budget on every task reply. Hard cap: $10/day.

**Worker concurrency.**
- `max_concurrent: 1` for all workers initially. MBP may graduate to 2 only after telemetry shows real queue depth. No async-I/O concurrency pattern.

**Retry policy.**
- One retry on `FAILED`/`TIMED_OUT` to a *different* worker of the same specialty if available, else terminal. Both episodes recorded.

**Discord UI.**
- One live-editing message per task with a per-subtask checklist (status emoji, worker name, adapter version). Streamed final synthesis at the bottom. Per-subtask thumbs via 🧵 thread reaction.
- Reward attribution: subtask thread thumbs → that episode (`user_score = ±1`, highest weight); main message thumbs → synthesis episode full weight + fractional credit (±0.3) to non-synthesis subtasks whose workspace keys synthesis read; no feedback in 24h → critic score is the reward.

**Refactor scope.**
- `agent/core.py` perceive→think→act→remember loop splits into `coordinator/orchestrator.py` (DAG dispatch + lifecycle) and `worker/executor.py` (single subtask → tool calls → result).
- `agent/safety.py` keeps per-worker deny-list as defense-in-depth; coordinator-side `shell` execution is the primary gate.
- `mesh/` reduced to discovery (Zyre announce of NATS URL); message types, signatures, and transport move under `transport/`.
- `learning/patterns.py` becomes Phase-A's exemplar miner.
- `discord_bot/` migrates to coordinator-only with the live-DAG message renderer added.
- New top-level modules: `transport/` (NATS), `coordinator/` (lifecycle, scheduler, registry, planner, budget), `worker/` (executor, capability advertiser, tool router), `learning/critic/`, `learning/trainer/`, `vault/`.

**Deep modules (each with a small, fake-friendly interface).**
- `TaskLifecycle`, `CapabilityRegistry`, `Scheduler`, `Planner`, `EpisodeStore`, `CriticQueue`, `BudgetGate`, `CapabilityToken`, `MessageSigner`, `AdapterRegistry`, `PromotionGate`, `VaultIndex`, `WorkspaceClient`, `TrainingJobBuilder`, `GoalScheduler`. Adapters for NATS / SQLite / embedding model / LLM client kept thin.

## Testing Decisions

**Definition of a good test in this codebase.**
- Tests describe externally observable behavior of a deep module in terms of its public interface. They do not assert against internal data structures, private methods, or call sequences to collaborators that could legitimately change without changing semantics.
- LLM, NATS, embedding, and SQLite are dependencies driven through narrow interfaces; unit tests pass fakes. Integration tests for those adapters live separately and can be marked `slow`.
- Prior art: `tests/` already uses in-memory SQLite (`:memory:`) and mock LLM providers returning canned `LLMResponse` objects (per CLAUDE.md). New tests follow the same pattern.

**Modules with required unit-test coverage (drive with fakes/in-memory):**
- `TaskLifecycle` — given a sequence of events, asserts state transitions, terminal classification, retry eligibility, recovery-after-restart.
- `CapabilityRegistry` — manifest insert/update/expire, `find_workers(specialty, required_tools, exclude_busy)` matching and tie-breaking by eval score.
- `Scheduler` — given a DAG + registry + in-flight counters, asserts dispatch decisions, queueing, retry-to-different-worker, NEEDS_SUBTASK extension.
- `Planner` — DAG topology validation (no cycles, depends_on references valid, required_tools subset of any worker's manifest); LLM mocked to return fixture DAGs.
- `EpisodeStore` — append, query by specialty/score/recency, dataset-extraction queries that Phase B/C will consume.
- `BudgetGate` — pre-call estimate, refusal at cap, midnight reset, fallback signal; clock and spend tracker injected.
- `CapabilityToken` — sign/verify, scope enforcement (regex match, cwd, timeout, expiry), tamper rejection.
- `MessageSigner` — Ed25519 round-trip; rejection of forged sigs and replays via request_id deduplication window.
- `AdapterRegistry` — manifest verification, SHA256 mismatch rejection, signature verification, staged → live transitions.
- `PromotionGate` — eval delta against incumbent, ≥2% margin, K-worker confirmation logic, halt-on-regression.
- `TrainingJobBuilder` — episode query → dataset JSONL, content-addressed dedup (same dataset never trains twice), job-spec field correctness.
- `GoalScheduler` — idle-window logic, budget enforcement, cadence respect, monthly cap exhaustion.

**Modules with mocked-collaborator tests:**
- `CriticQueue` — async queue ordering and backpressure with a fake LLM client; calibration sampling rate verified statistically.
- `VaultIndex` — markdown ingest + query with a deterministic fake embedder; commit-watcher updates index.
- `WorkspaceClient` — interface contract verified against an in-memory fake; integration test against a real NATS in `tests/integration/`.

**Phase-gated, not built in Phase 0:**
- Discord live-DAG renderer: visual smoke test only initially; reward attribution logic in `Coordinator` tested without Discord in the loop.
- Trainer: integration tested by running a small toy LoRA on a CPU fixture; full H100 path is operationally validated, not unit-tested.

**Eval set authoring** (Phase 1 deliverable): `evals/research-summarize/*.jsonl` seeded with 30+ hand-written cases covering claim preservation, voice match, citation correctness. Eval cases are not "tests" of the module; they are the β-promotion gate's data.

## Out of Scope

- HA / failover for the coordinator (single-operator cluster; restart on crash).
- Multi-tenant / multi-user Discord (solo operator).
- Public NATS exposure (LAN-only initially; design admits public flip but it's not built).
- Code, vision, sensor, or other non-research specialties (emerge later, demand-driven).
- Self-play / synthetic-task generation (mechanism δ; deferred indefinitely).
- Worker hot-swap of base models on demand (only adapters swap; base models pinned per worker until reprovisioned).
- Pi 3 and Surface Pro as workers (Pi 3 lacks RAM; Surface Pro deferred until cluster is stable).
- Cluster-wide structured log aggregation (Loki/Vector); local node logs sufficient initially.
- Worker self-update protocol — manual SSH/ansible for now.
- Dashboard / kanban board UI — deferred until episode-curation pain warrants.
- Key rotation (registry stores key IDs to admit rotation later, but rotation flow not implemented).
- Phase D (online RL with rollouts) infrastructure.

## Further Notes

- **First user-facing milestone (Phase 0 exit):** Submit a Discord message → coordinator plans a 2-subtask DAG → two workers execute in parallel → synthesis returns → episode rows in SQLite. Nothing learns yet; the spine works.
- **First "the cluster did something cool" milestone (Phase 1 exit):** Auto-research γ produces 3–5 useful inbox notes per week with zero cloud spend; operator triages weekly.
- **First "the cluster improved itself" milestone (Phase 3 exit):** A `research-summarize` LoRA adapter trained on H100 from operator-graded episodes ships to a Jetson, wins held-out eval by ≥2%, and survives K-worker live re-eval.
- **Cost model:** Realistic daily cloud spend $0.50–$3 on an active day; the $10/day cap is a safety net, not the operating point. Critic calibration ~$0.001/task, complex planner calls ~$0.05, occasional Sonnet/Opus synthesis $0.05–$0.30.
- **Subscriptions vs API:** Claude Pro/Max as an interactive escape hatch is fine, but is not load-bearing for autonomous loops. The cluster's autonomy assumes API access with the budget gate in front of it.
- **Vault is the asset.** The cluster's daily value is "my Obsidian vault gets smarter while I sleep." Every architectural choice should be evaluated against whether it makes the vault grow more usefully, not against whether it adds capability.
- **Training facility communication:** H100/DGX runs `turing-trainer` as a systemd unit dialing the coordinator's NATS outbound. No inbound connection to the facility. Facility never runs shell, never joins the runtime mesh, never holds a worker key.
- **Manual training-job approval initially**, graduating to autonomy within per-specialty quotas as trust accumulates. Failed promotion attempts are themselves data tracked over time; if win-rate drops below 30% on a specialty, the dataset/eval is the bug.
- **Stability tolerance:** A fresh fine-tune may briefly produce worse outputs before improving. Operator's stance is "let it run a day, don't roll back at first regression." K-worker re-eval halt-on-catastrophic-regression remains.
- **The Pi 3 problem and the Surface Pro:** explicitly noted not to provision them as workers. Document this in operational runbook so a future contributor doesn't spend a weekend trying to fit a 3B model into 1GB.
