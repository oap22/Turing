# ADR 0009 — Jetson-only fleet retarget + the human-curated research flywheel

- Status: Proposed
- Date: 2026-05-29
- Relates to: PRD #1 (coordinator/worker reshape), ADR 0001 (cluster topology),
  ADR 0004 (critic / judge specialty), ADR 0007 (adapter promotion canary),
  ADR 0008 (NATS presence). Supersedes the hardware assignments in `CONTEXT.md`
  "Topology" and amends the Phase B trainer in "Self-improvement phases".

## Context

Two things changed since ADRs 0001–0008 were written, and both invalidate
load-bearing assumptions:

1. **The hardware fleet changed.** The Pis (incl. the Pi 5 coordinator) and the
   MacBook Pro are gone. The operator now has:
   - **4× Jetson Orin Nano Super Developer Kits** (8 GB unified LPDDR5 each,
     6-core Arm A78AE + 1024-core Ampere GPU, MAXN SUPER mode confirmed).
   - **A Surface Pro** (16 GB, Windows) used as the always-on host, reached over
     SSH.
   - **Pull-only H100/DGX** training facility (unchanged from ADR 0001).

   This breaks three concrete decisions in `CONTEXT.md`: the **Pi 5 coordinator**
   (gone), the **MBP premium worker hosting the critic 13B+ and the MLX SFT
   trainer** (gone — and MLX is Apple-Silicon-only, so it cannot move to Jetson
   CUDA), and the line **"Surface Pro excluded"** (now the coordinator host).

2. **The operating model changed.** The PRD's always-on real-time
   coordinator/critic/canary machinery is heavier than the operator wants for
   Phase 0. The new model is a **nightly batch + morning human curation** loop:
   the swarm researches overnight; the operator reviews the output the next
   morning with Claude Code and decides what becomes training data. The human is
   the orchestrator and the critic until the system earns more autonomy.

The overarching goal also sharpened: **fine-tune the four Jetsons into AI/ML
"mini-experts"** via a self-improving data flywheel — seed research questions →
research them → expand into new questions → curate → fine-tune → repeat.

This ADR records the hardware retarget, the Phase 0 operating model, and the
fine-tuning guardrails, which are grounded in a literature review (cited below)
because the naïve version of this loop is known to fail.

## Decision

### 1. Node assignments (replaces `CONTEXT.md` "Topology" → "Node assignments")

| Node | Role |
|------|------|
| **Surface Pro (16 GB), Windows + WSL2 Ubuntu** | Always-on **coordinator** + diagnostics console (webui) + **operator morning-review surface** (Claude Code over SSH). Runs **no local LLM inference**. |
| **4× Jetson Orin Nano Super (8 GB)** | Homogeneous local **research workers**. Run quantized local models (realistically 3–8 B at Q4 within 8 GB shared CPU+GPU memory). |
| **H100 / DGX** | **Pull-only fine-tuning facility.** The only place adapter training happens. Subscribes to coordinator NATS via `turing-trainer`; never dials in; never joins the runtime mesh. |
| **Cloud Claude API** | Teacher/polisher for fine-tuning targets + the operator's morning copilot. |

**Why the coordinator runs in WSL2, not native Windows.** The entire safety
model is Linux-shaped: the shell safety gate, the `rm -rf /`-class deny-list
patterns, `asyncio.create_subprocess_shell`, and the `systemd` deploy units
(`turing-trainer`). Native Windows would force a rewrite of the most
security-critical code in the project. WSL2 Ubuntu keeps Linux semantics intact
while the Windows box provides the always-on host. Requirements:

- Windows power: never sleep, "do nothing on lid close", always plugged in. A
  coordinator that sleeps takes the whole fleet's brain offline.
- `/etc/wsl.conf` → `[boot] systemd=true` so the `systemd` units run.
- WSL2 is NAT'd by default; the Jetsons cannot reach it. Use **mirrored
  networking mode** (`.wslconfig` on Win11) or a port-proxy so the coordinator's
  NATS/gateway ports are reachable on the LAN. (Known gotcha — call it out in
  deploy docs.)

**Why the Surface as coordinator and not a Jetson.** The coordinator does no
heavy inference — it is orchestration, scheduling, the Discord bot, the vault
index, the SQLite episode store, the budget gate, and the webui (which *is* the
diagnostics surface the operator asked for). 16 GB is ample for that. Keeping it
off the Jetsons preserves **all four** Jetsons as workers (the operator's stated
"max workers" goal). The trade-off accepted: a laptop is a less reliable
always-on device than a headless SBC; mitigated by the power settings above.
Revisit if lid-sleep reliability proves to be a problem in practice — promoting
one Jetson to coordinator (3 workers) is the fallback.

### 2. Phase 0 operating model: nightly batch + morning curation

- **Night.** The coordinator dispatches a **human-gated question queue** to the
  Jetsons over NATS. Each worker runs the existing executor loop (Perceive →
  Think → Act → Remember), **grounding answers in fetched external sources**
  (`web_fetch` allowlist), and may **propose follow-up questions** into a
  `proposed` queue (it does not auto-pursue them in Phase 0). Drafts land as
  `vault/inbox/<task_id>/<slug>.md` with the existing frontmatter, plus the
  fetched source list. Every closed subtask is still an **episode** row.
- **Morning.** The operator opens the inbox with **Claude Code** over SSH,
  reviews drafts + sources, edits, and **accepts / rejects** each. Accepts are
  promoted to the curated vault; the polished, reasoning-bearing answer becomes
  a candidate SFT pair. Rejects and edits are logged. **These human decisions
  are the Phase 0 reward signal.**
- **Frontier control.** Proposed follow-up questions are **human-gated**: the
  operator approves which ones enter the next night's queue. This is relaxed
  toward "autonomous within caps" only once the operator trusts the system and
  dedup/relevance filters are in place.

This **replaces, for Phase 0**: the real-time critic queue (ADR 0004 — the human
is the critic), the Discord live-editing reward UI (ADR 0006 — deferred; morning
curation supplies reward instead), and the always-on automated canary loop (ADR
0007 — see §4, the eval gate still applies to training but is run as part of the
nightly/periodic H100 job, not a live fleet service).

### 3. Specialty strategy: generalist first

All four Jetsons train toward **one shared AI/ML-generalist adapter, replicated
×4**. Rationale: early on, curated data is scarce; splitting it four ways starves
each adapter (this is exactly why `CONTEXT.md`'s Phase B started with a single
specialty). One adapter also gives 4× throughput on the same work and fault
tolerance (any node does any task). **Split into AI/ML sub-domain specialists
later**, once there is enough curated per-domain data to justify separate
adapters. The existing per-specialty machinery (per-specialty evals, lessons,
adapter registry) is preserved for that later split.

### 4. Fine-tuning loop and guardrails (the load-bearing part)

A literature review (see "Evidence base") makes the failure modes of this loop
concrete. The following are **non-negotiable design rules**, each tied to its
evidence:

| Rule | Why | Source |
|------|-----|--------|
| **Knowledge must enter from outside the student model.** Targets are grounded in fetched real sources and/or polished by a stronger teacher (cloud Claude) — never the 7-8 B's own unaided output. | Recursive self-distillation collapses; "echo chamber" amplifies the small model's blind spots. | Curse of Recursion (arXiv:2305.17493), Nature 2024, Self-Consuming Models Go MAD (2307.01850) |
| **Always train on *accumulated* real + synthetic data; never replace.** Keep a frozen real seed set + the fetched sources in every training run. | Under "replace", error grows unbounded; under "accumulate", error has a finite bound independent of iterations. No agreed minimum mixing ratio — the accumulate-vs-replace *strategy* dominates. | Is Model Collapse Inevitable? (2404.01413), Collapse or Thrive? (2410.16713) |
| **Capture reasoning / rationale, not just the final answer**, as the SFT target. | Style-only imitation transfers fluency but not factuality; process/rationale distillation is what actually raises capability. | False Promise of Imitating Proprietary LLMs (2305.15717) vs Orca (2306.02707), Distilling Step-by-Step (2305.02301) |
| **Question expansion = in-depth + in-breadth + an explicit *elimination* step.** Discard low-information, copied, or unanswerable evolutions. | The discard step is the mechanism, not an afterthought, in the one validated recipe. | Evol-Instruct / WizardLM (2304.12244) |
| **Dedup the question frontier** by embedding/ROUGE similarity before it enters the queue. | Prevents redundancy and combinatorial drift. | Self-Instruct ROUGE-L < 0.7 gate (2212.10560) |
| **Re-train the LoRA from the base model each cycle on the accumulated set; do not stack adapters iteratively.** | Iterative self-training overfits the small training set and *regresses* — ReST-EM regressed on coding after iteration 1. | ReST-EM / Beyond Human Data (2312.06585) |
| **Expect 1–3 useful rounds, then re-evaluate — not perpetual gains.** | Every self-improvement loop that measured it saturates fast; none demonstrate indefinite improvement. | ReST-EM (2312.06585), Self-Rewarding LMs (2401.10020) |
| **Curate ruthlessly; quality + diversity ≫ quantity.** A small curated set beats a large noisy one. | LIMA: 1,000 curated examples rival GPT-4; 16× more data gave no gain without diversity. QLoRA: a 9.8 K set beat a 450 K set; dataset *choice* moved MMLU 1.5–8 pts, *size* moved it 0–0.5. | LIMA (2305.11206), QLoRA (2305.14314) |
| **LoRA on *all* linear layers, modest rank, few epochs.** | Adapter placement matters more than rank for matching full-FT quality; few epochs guards small-data memorization. | QLoRA (2305.14314) |
| **Eval-gate on held-out *real* data + diversity/tail metrics, not mean accuracy alone.** | Collapse erodes diversity and distribution tails *before* mean accuracy drops — a mean-only gate misses early collapse. | Nature 2024, Measuring Diversity in Synthetic Datasets (2502.08512), Synthetic Eggs in Many Baskets (2511.01490) |
| **A/B the polisher/teacher size; biggest is not always best for a 7-8 B student.** | "Larger Models' Paradox" — teacher-student compatibility matters; a mid-size teacher can transfer better. | Stronger Models are NOT Stronger Teachers (2411.07133) |

This retains the spirit of ADR 0007's eval gate (held-out improvement before
promotion) and extends its metric set to include diversity/tail coverage.

### 5. Rust (scope note, not Phase 0)

Rust is **additive and narrow**, not a rewrite. Two candidates, both deferred and
gated on measurement:

- A **CLI/TUI operator surface** (the CLI was already "out of scope for Phase 0";
  this is a new front-end, a natural Rust fit, no Python-ecosystem dependency).
- **Perf hotspots** behind a profiler result — most plausibly the message
  signing/dedup path in `transport/`, exposed to Python via PyO3/maturin. The
  agent loop, LLM clients, and orchestration stay in Python.

No core logic is rewritten. The thin deterministic spine described for the
coordinator is the only code that would ever be a Rust target, and only later.

## Consequences

### What changes in the docs / design

- `CONTEXT.md` "Topology", "Node assignments", "Module layout", "Self-improvement
  phases", and "Operator surfaces" are updated to the Jetson-only fleet and the
  morning-curation model (this PR).
- **Phase B trainer:** `learning/trainer/` "MLX SFT (MBP)" → **CUDA LoRA on the
  H100/DGX**. MLX is removed (no Apple Silicon in the fleet). Phase B and Phase C
  now share the same H100 home; the distinction becomes SFT (Phase B) vs DPO
  (Phase C), not MBP vs H100.
- **Critic (ADR 0004):** for Phase 0 the critic is **human morning curation**.
  The automated judge specialty is deferred; when reintroduced it runs as a
  **cloud-API judge call** (budget-gated), not a local 13 B — no node can host
  13 B in 8 GB. ADR 0004 is amended, not discarded.
- **Discord reward UI (ADR 0006):** deferred. Reward comes from morning curation
  logged against episodes. Discord remains available for notifications.

### What we keep

- The episode store as the training corpus (unchanged and now more important —
  it must log both the overnight run *and* the morning human decision per
  episode, or the path to autonomy has no training data).
- The vault inbox flow (`vault/inbox/**` as the only writeable path) — it *is*
  the morning curation queue.
- NATS as the runtime bus (ADR 0008 presence subjects), capability tokens (ADR
  0003), Ed25519 message signing, `<untrusted_data>` wrapping, the `$10/day`
  budget gate, and the eval-gated promotion principle (ADR 0007).

### What we accept as risk

- **Laptop-as-coordinator reliability.** Mitigated by power settings; fallback is
  promoting a Jetson to coordinator.
- **WSL2 networking friction.** Mirrored mode / port-proxy required; documented.
- **Saturation of the flywheel.** Expected and bounded by the "1–3 rounds, then
  re-evaluate" rule; not treated as a perpetual-motion machine.

## Evidence base

Prior art (results): Self-Instruct (arXiv:2212.10560), Stanford Alpaca
(crfm.stanford.edu/2023/03/13/alpaca.html), Unnatural Instructions
(2212.09689), Evol-Instruct/WizardLM (2304.12244), WizardMath (2308.09583),
WizardCoder (2306.08568), STORM (2402.14207), DeepResearchGym (2505.19253),
Orca (2306.02707), Orca 2 (2311.11045), Textbooks Are All You Need / phi-1
(2306.11644), Distilling Step-by-Step (2305.02301), STaR (2203.14465), ReST
(2308.08998), ReST-EM (2312.06585), Self-Rewarding LMs (2401.10020),
Constitutional AI / RLAIF (2212.08073).

Pitfalls: Curse of Recursion (2305.17493), Nature 2024 model collapse
(nature.com/articles/s41586-024-07566-y), Self-Consuming Models Go MAD
(2307.01850), False Promise of Imitating Proprietary LLMs (2305.15717),
diversity collapse (2511.01490, 2510.01171, 2502.08512).

Mitigations: accumulate vs replace (2404.01413, 2410.16713), LIMA (2305.11206),
QLoRA (2305.14314), Stronger Models are NOT Stronger Teachers (2411.07133).

Contested / flagged: WizardLM "beats ChatGPT" holds only on a self-curated hard
subset (loses overall); Self-Rewarding gains came with a 3× response-length
growth (length-bias / reward-hacking flag) and did not appear on math/reasoning;
phi/Orca "small matches large" headlines carry open benchmark-contamination
concerns; "model collapse is inevitable" is contested (replace-regime artifact
vs statistical inevitability). Treat all single-benchmark numbers as
direction-of-effect, not guarantees.

## Out of scope

- The autonomous (no-human) frontier mode and the automated critic re-activation
  — separate ADRs once the human-gated loop has produced training data.
- DPO specifics on the H100 (Phase C details).
- The Rust CLI/TUI and any PyO3 hotspot — separate, measurement-gated work.
- Multi-specialty split mechanics — deferred until per-domain data justifies it.
