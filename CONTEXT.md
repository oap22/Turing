# Turing — Project Context

Domain language and load-bearing decisions for the Turing multi-edge research cluster. Read this before working in the repo; it's the shared vocabulary every ADR, issue, and module assumes.

## What Turing is

A personal AI cluster that runs domain research overnight and grows an Obsidian vault into a smarter knowledge base over time. The target expertise area is **AI/ML** — the workers are being fine-tuned into AI/ML "mini-experts". Homogeneous edge hardware (4× Jetson Orin Nano Super, 8 GB) fronted by an always-on Surface Pro coordinator, plus pull-only access to an H100/DGX training node. Local-first by design; cloud (Claude API) reserved for hard tiers under a hard $10/day budget.

`main` is a coordinator + worker cluster with episodes, critic, and a self-improvement pipeline — the PRD (#1) reshape, landed across ADRs 0001–0010. The original single-agent Discord bot has been **retired** (ADR 0010): the webui is now the operator surface and ntfy carries closed-laptop alerts. ADRs in `docs/adr/` lock the boundaries before code moves. **ADR 0009 retargets the fleet to Jetson-only + Surface/WSL2 and defines the Phase 0 human-curated research flywheel — read it alongside this file.**

## Topology

- **Coordinator** — single always-on node (**Surface Pro, 16 GB, Windows + WSL2 Ubuntu**). Owns: task lifecycle, scheduler, capability registry, planner, budget gate, vault index, gateway (webui + queue/chat API), alerts dispatcher (ntfy), episode store, critic queue dispatch, adapter registry. Runs **no local LLM inference** — it is orchestration + diagnostics + the operator's morning-review host. Runs in **WSL2** to preserve the Linux safety model (shell gate, deny-list, `systemd`); see ADR 0009 for the power-settings and mirrored-networking requirements.
- **Worker** — 4× **Jetson Orin Nano Super (8 GB)**, homogeneous. Owns: executor (the agent loop), capability advertiser, tool router, low-risk tool execution. Run quantized local models (~3–8 B at Q4 within 8 GB shared memory). Workers never run the coordinator services (gateway, scheduler, planner), never hold the shell safety gate.
- **Training facility** — H100/DGX. Pull-only via `turing-trainer` systemd unit subscribed to coordinator NATS. No inbound dial. Never joins runtime mesh. **The only place fine-tuning happens** (the 8 GB Jetsons cannot train).
- **Node assignments** — Surface Pro = coordinator (the old "Surface Pro excluded" line is reversed by ADR 0009); 4× Jetson Orin Nano Super = workers; H100/DGX = pull-only trainer. **No Pis, no MacBook Pro** — both retired from the fleet.

## Runtime bus

- **NATS JetStream** is the runtime bus for all task / result / event traffic. LAN-only, TLS + nkey auth. Config admits a public flip later.
- **Pyre/Zyre is discovery-only** — announces the coordinator's NATS URL on the LAN. No task or result traffic ever rides Zyre. The legacy `mesh/` module collapses into `transport/` as a discovery shim.
- Every `MeshMessage` is Ed25519-signed by sender, verified by receiver. Replay protection via `request_id` dedup window.

## Module layout (post-refactor)

```
transport/          NATS client wrapper, MessageSigner, Pyre discovery shim
coordinator/        lifecycle, scheduler, registry, planner, budget, vault index, alerts (ntfy)
gateway/            FastAPI webui host — queue-manager + chat panes, WS frames, alerts SPA
worker/             executor, capability advertiser, tool router
learning/critic/    async critic queue, Haiku calibration, drift detector (Phase 0: human morning curation; automated judge = cloud-API, deferred)
learning/trainer/   CUDA LoRA SFT (H100/DGX), DPO entry points, dataset builder
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

In Phase 0 the **critic is the operator's morning curation** (see "Operator surfaces"): accept/reject/edit decisions are the reward signal, written back to the episode row. The automated `judge`-specialty critic is deferred; when reintroduced it runs as a **cloud-API judge call** (budget-gated), not a local 13B — no 8 GB node can host one. (ADR 0004 amended by ADR 0009.) The `learning/critic/` code (RemoteCritic, retry queue, wiring) is **dormant pre-0009 scaffolding** — it dispatches to a judge *worker* and is not wired into the Phase 0 runtime; reactivating it requires retargeting the dispatch to the cloud-API call first. Nightly at 02:00, top-K-by-score episodes per specialty are condensed into **lessons** — short, structured, vector-indexed, specialty-tagged. Workers retrieve top-3 lessons by `(specialty, prompt)` similarity and the executor renders them as a `<lessons>` block in the user message. Lessons TTL out at 60 days unless **pinned** by the prompt evolver citing them in a winning A/B prompt; pin renews on re-citation, lapses otherwise.

## Lifecycle

`PENDING → DISPATCHED → RUNNING → {COMPLETED, FAILED, TIMED_OUT, REJECTED, NEEDS_SUBTASK}`. Every transition appended to JetStream subject `tasks.<id>.events` with a SQLite projection. Subtask execution idempotent on `subtask_id`.

## Self-improvement phases

- **Phase A** — nightly prompt + few-shot exemplar evolution. Zero training compute. Reuses `learning/patterns.py`.
- **Phase B** — **H100/DGX CUDA LoRA SFT** (was MBP MLX; MLX retired with the MacBook). First target: a single AI/ML generalist adapter, replicated across all 4 workers. Eval-gated promotion.
- **Phase C** — H100 DPO via `turing-trainer`. Same eval gate.

**The research flywheel (ADR 0009).** Seed AI/ML questions → workers draft answers grounded in fetched external sources → cloud Claude polishes the keepers → operator curates in the morning → curated (question, reasoning, answer) pairs **accumulate** with real seed/source data → H100 trains a LoRA from base each cycle → adapter pushed to workers → repeat. Guardrails are non-negotiable and literature-backed (see ADR 0009): knowledge must enter from outside the student model; **accumulate, never replace** training data; capture reasoning not just answer style; re-train from base each cycle (don't stack adapters); expect **1–3 useful rounds then re-evaluate** (the loop saturates); LoRA on all linear layers with few epochs; **eval-gate on diversity + distribution tails, not mean accuracy alone**; dedup the question frontier. Question expansion uses in-depth + in-breadth + an explicit elimination step; the frontier is **human-gated** in Phase 0, relaxed toward autonomous-with-caps only as trust grows.

**Bench cycle.** A software-only run of the entire flywheel on a dev machine — the test-bench idiom: every seam is real (question queue, dispatch, inbox, morning curation, reward write-back, dataset build, adapter registry, eval harness), only the three edges are synthetic (worker LLM → canned drafts; trainer → signed stub adapter; operator → scripted accept/edit/reject). Always runs **two cycles back-to-back**, because the non-negotiable guardrails (accumulate-never-replace, retrain-from-base, frontier dedup, pin/TTL renewal) are cross-cycle properties invisible in a single pass; cycle 2 seeds partly from cycle 1's worker-proposed follow-ups and is rigged to fail canary, so the full rejection chain (REJECTED → revert → `hard_examples` → next-cycle dataset) is proven before it ever runs with a real adapter at stake. A bench cycle is *not* a dry run — its side effects (git commits, file moves, reward rows) are the point; they land in fixture sandboxes. Passing means hard assertions hold *and* the artifact report is inspectable.

**Promotion gate.** Offline ≥2pp held-out improvement → `STAGED` in `AdapterRegistry`. K=1 round-robin canary worker re-runs the held-out eval at quantization; pass = `score ≥ prior_live_canary_score − 0.5pp` → fleet rollout to LIVE. Fail → permanent `REJECTED`, canary reverts, worst-failed cases written to `hard_examples` for next-cycle training; a rejection notice with the per-specialty 30-day regression rate feeds the morning-review flow and surfaces in the webui.

## Safety boundaries

- **Shell runs on the coordinator only.** Workers emit signed `tool_request`; coordinator verifies a capability token (`subtask_id`-scoped: `allowed_commands_regex, cwd, timeout, expires_at`) before executing. One audit log, one gate.
- Workers execute low-risk tools locally: `vault_query`, `workspace_io`, `web_fetch` (allowlist), specialty tools.
- Adapters verified by SHA256 + Ed25519 signature on load. Mismatched-base or tampered adapters refused.
- Cross-DAG outputs wrapped in `<untrusted_data>` blocks to mitigate indirect prompt injection.
- **$10/day cloud budget**, hard cap. `BudgetGate` pre-call estimate, refusal at cap, midnight reset, fallback signal. Remaining budget is surfaced through the webui (`format_remaining_budget`).

## Vault

The vault is a **private git repository** (ADR 0010 §4). The coordinator commits on curated promotions, so the commit log is the reward-signal audit trail. `turing-vault-watcher.service` hosts a vector index of the operator's Obsidian vault and reindexes by **diffing each new commit against its parent and reindexing only the changed markdown files** (added/modified re-embedded, deleted dropped, renames handled) — no full resweep. The coordinator's WSL2 working tree is the **single writer**; the Mac and other devices are pull-only git remotes, and external Obsidian Sync of the same vault is incompatible. See `docs/operator/vault-git-workflow.md`. Workers ground via `vault_query(query, k)`. Workers propose new notes via `vault_propose` → `vault/inbox/<task_id>/<slug>.md` with frontmatter (`source, task_id, specialty, confidence, critic_score`). Curated vs inbox is purely path-based — `vault/inbox/**` is the only writeable path.

## Operator surfaces

Turing's operator surfaces, each owning a distinct role (the webui is primary since the Discord retirement — ADR 0010):

- **Webui** (`webui/`, served by the gateway). **The primary operator surface** (ADR 0010 §1) — it owns both fleet observability and work direction. Observability: a live call-graph of cross-node tool dispatch, a filterable message-trace pane, and the fleet specs panel (per-node CPU%, mem, disk, temp, uptime, loadavg with hardware identity); rows colour-shift toward warn/danger as values approach hardware-risk thresholds and dim when a peer's last heartbeat is stale. Work direction: a **question-queue manager** for the human-gated frontier (`proposed → approved → in-flight → drafted → curated`, the hero surface) plus a **chat pane** for ad-hoc task submission. Reward attribution is preserved verbatim from the retired Discord surface: subtask thumb → that episode (±1.0); synthesis thumb → synthesis episode (±1.0) + fractional credit (±0.3) to subtasks whose `output_key ∈ synthesis.consumed_keys`; no feedback in 24h → `critic_fallback` reward in `[-0.3, +0.3]`. Rewards are *events* in `episode_rewards(episode_id, source, value, recorded_at)`; effective reward is `SUM(value)` per episode.
- **Discord** — **retired** (ADR 0010 §3). It was the work-direction surface; that role moved to the webui queue manager + chat pane, and the closed-laptop alert fallback moved to ntfy. The task bot, surface writer, reaction listener/reconcile, cogs, and views are deleted; `discord.py` is dropped and all `TURING_DISCORD_*` config removed. The `discord_*` columns in `episode_rewards` are retained as historical reward-source provenance (effective reward is still `SUM(value)`, source-agnostic).
- **Morning review (Claude Code over SSH)** — the Phase 0 *operating* surface and the primary reward source. Overnight the swarm researches a human-gated question queue and writes drafts (plus fetched sources) to `vault/inbox/`; each morning the operator opens the inbox with Claude Code on the Surface, reviews drafts + sources, and accepts / rejects / edits. Accepts promote to the curated vault and become candidate SFT pairs; all decisions are logged against episodes as the reward signal. The operator is the orchestrator and the critic until the system earns more autonomy. (ADR 0009.) Worker-proposed follow-up questions reach the dispatch frontier **only** through the morning **frontier review** (`flywheel/frontier_review.py`): approving a proposal promotes it from the holding `ProposedQueue` into the runnable `QuestionQueue` (carrying `origin_question_id` provenance), declining records the rejection — that approval *is* the Phase 0 human gate and is the single path into the runnable frontier. **This flywheel queue is a separate projection from the webui question-queue manager** (ADR 0010 Slice C); the two are deliberately left unreconciled for now rather than growing a third divergent approval path.
- **TUI** (`tui/`, `turing-tui`) — a **Rust + ratatui** terminal operator surface, the keyboard-first counterpart to the webui. Additive front-end (not a rewrite): a standalone client of the **same gateway HTTP/WS API**, mirroring the queue / chat / specs / trace / alert panes, with byte-identical curation reward semantics (the affordance changes, the reward rows do not). Chosen for latency — native render loop, event-driven redraws (idle CPU ≈ 0), all socket I/O off the render path. Honours ADR 0009 §5's "narrow Rust use is the CLI/TUI; the agent loop and orchestration stay in Python." Connect over the Tailnet with `TURING_GATEWAY_URL` + `TURING_GATEWAY_TOKEN`. See `tui/README.md` and `docs/operator/connectivity.md`. (Measurement-gated perf hotspots via PyO3 remain a separate future option.)

### Alerts

Hardware-safety alerts (PRD #228) watch every peer's `specs.temp_celsius` and `disk_pct` for sustained warn/danger conditions and push a notification when waiting for the next visual check would be too late. Per `(peer, field)` a small state machine runs `clear → pending → alerting`: it takes N consecutive warn-or-worse heartbeats to fire (N=3 for warn, N=2 for danger) and N=3 ok heartbeats to clear, so a brief spike never flaps the banner. The SPA is the primary channel — an `alert` WebSocket frame drives a persistent top banner — with **ntfy** push as the closed-laptop fallback (self-hosted on the Surface coordinator; topic per operator): on the `alerting` edge, if the gateway's telemetry sink has had no successful push within the last 90 s (`last_send_ms` older than 90 s, or `None` because no SPA has ever connected), the dispatcher POSTs the one-line summary to the operator's ntfy topic at `{TURING_COORDINATOR_NTFY_BASE_URL}/{TURING_OPERATOR_NTFY_TOPIC}` (`coordinator/alerts/ntfy_client.py`). The Discord-DM fallback is **retired** by ADR-0010 §2: ntfy replaces it. The transport lives entirely behind a tiny defensive client — an ntfy push that fails (server down, non-2xx, transport error) is caught and dropped, and never clears or bypasses the snooze map. The operator can `snooze 4h` any banner row; the snooze is keyed by `(peer, field)`, suppresses both SPA frames and ntfy pushes for the window, and is in-memory by design — a coordinator restart clears every snooze. Scope is deliberately safety-only — TEMP and DISK, the two fields with no recovery path that doesn't run through the operator's hands.

## Issue tracker

GitHub Issues at `oap22/Turing`, managed via `gh`. Triage labels: `needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix`. ADRs are `ready-for-human` because they need operator decisions; slices are `ready-for-agent`.
