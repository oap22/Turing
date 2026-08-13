---
status: agreed
date: 2026-08-12
project: Turing
---

# Research Brief — Turing as an autonomous, self-editing research agent

> **Agreed 2026-08-12** after a restatement pass that drew no corrections. Any
> later correction returns this to `draft`. A materially changed question gets a
> new dated brief; this one is left in place as the record.

## The pivot (recorded first, because it invalidates prior docs)

Turing stops being "fine-tune four Jetsons into AI/ML mini-experts" (ADR 0009)
and becomes **a general research agent**: given a problem, it works autonomously
until the result passes a human-supplied verifier, and it **edits its own
scaffold** between projects to get better at doing so.

What this pivot kills or demotes:

- **ADR 0009's specialty strategy, generalist adapter, Jetson-worker premise, and
  human-curation reward loop** — all served the old target. Needs a superseding ADR.
- **Issue #379** shrinks from "the retarget" to "one possible task family."
- **The local LoRA flywheel** has no job under this framing unless re-chartered as
  *cost reduction* (distilling scaffold behaviour onto local models). Downstream.
- **`BudgetGate`'s dollar metering** — retired at Owen's direction (§ Budget).

What survives: the episode store, the safety model (shell gate, capability
tokens, Ed25519 signing, `<untrusted_data>` wrapping), the promotion/canary
rollback machinery, and ADR 0009's guardrail list as background knowledge.

## Question

**Does an agent that rewrites its own scaffold between projects improve its
solve rate on a held-out corpus of verifier-bearing ML projects — by how much
per round, at what cost, and where does it stop?**

**Kill conditions** (any one ends the project):

1. Self-edits never beat the frozen-scaffold baseline beyond the noise floor.
2. The agent games the harness — gains traced to reward hacking, not capability.
3. Round-0 solve rate is near zero, so there is no signal to act on.

**Explicitly NOT fatal:** saturation at round 1–2. That is what ADR 0009's own
evidence base predicts (ReST-EM 2312.06585, Self-Rewarding LMs 2401.10020) and
`driving-functions.md` reiterates. **The deliverable in that case is where it
saturates, why, and what moves that point.** Owen changed this during the
interview after Claude flagged that marking it fatal was a pre-commitment to
abandon on the most likely outcome.

## Hypotheses

- **H1** — A frozen-scaffold Claude agent solves a non-trivial, non-saturating
  fraction of the corpus (round-0 solve rate is in the difficulty band, neither
  ~0 nor ~1). *Refuted by:* a floored or ceilinged round 0. Closes at round 0.
- **H2** — Self-editing the scaffold produces a solve-rate gain over round 0
  exceeding the seed-noise floor. *Refuted by:* Δ within noise for the first two
  self-editing rounds.
- **H3** — Gains, where they occur, are capability rather than corpus-specific
  overfitting. *Refuted by:* primary climbs while the secondary axis flatlines
  or drops (Goodhart).
- **H4** — Human-gate load trends toward zero across rounds. *Refuted by:* it
  rises — in which case the loop is a treadmill, not self-improvement.

## Settled decisions

| Decision | Value |
|---|---|
| Unit of work | A project = `(goal, verifier)` pair |
| Verifier provenance | **Human-supplied, OR agent-proposed and human-approved before work begins** — frozen from that moment either way. The agent may never edit, relax, or regenerate the bar it is graded on. *(Amended later in the same interview. The freezing is the load-bearing property, not the authorship.)* Costs nothing per round: the corpus is locked before the noise floor, so every approval happens once, at setup. |
| Internal metrics | **The agent builds its own validation splits, diagnostics, and internal measures freely** — that is how it navigates a problem at all. None may ever count as the official score. *The agent invents; Owen holds the ruler.* |
| Problem shape | A research problem is **something with a number to move** — more accurate, faster, whatever the metric. Turing's core takes a goal plus a script returning a number and is indifferent to which. **Kaggle is one translator behind that interface, not the interface itself.** |
| Test-set variety | Owen wants a **mix** — Kaggle-type, speedup, and genuine research questions — not one narrow family. **Score each type separately; never average across types**, or a round that helps one and hurts another reads as flat. Accepted cost: ~3 problems per type in a 10–12 corpus is thin per-family signal. |
| Novelty | Owen wants the agent to "push the boundaries of what is currently done." **Recorded as an aspiration, explicitly unmeasured.** Nothing prevents it — the verifier never asks which technique was used. Nothing rewards it either: a metric-maximiser takes the cheapest path, which is normally a strong known method applied well. Measuring novelty mechanically is unsolved, not deferred. **Not a success criterion; do not read it into results after the fact.** |
| Corpus | Existing ML benchmark harness (MLE-bench / Kaggle-style: goal + held-out set + metric ship as a package) |
| Improving artifact | **Code — the scaffold edits itself** between projects |
| Scaffold = | **Skills.** Self-edits land as skill files; a git SHA is the scaffold version per round, `git revert` is rollback, a diff is the answer to "what changed". |
| Scaffold repo | **A dedicated `turing-skills` repo, forked from `~/Developer/active/skills` at a frozen commit.** Owen's daily skills and the measurement layer (`research-loop`, `driving-functions.md`, `log_run.py`) are **read-only** to the agent. Divergence from the personal repo is the accepted cost. |
| Editable surface | Whole Turing repo + `turing-skills` writable; harness, verifier, scorer, eval data, and measurement skills in separate read-only repos |
| Engine | **Claude only, on Owen's subscription** (revised 2026-08-12). Opus orchestrates; Haiku-tier handles genuinely mundane sub-steps. **No local model in the loop.** |
| Local-model seam | Build **one backend interface** — send messages + tools, get a response — with a Claude implementation today and a local one droppable in later as backup. The MLX research below stays as reference for whoever builds that backend. Not implemented now. |
| Why this is better for the science | A split Claude/local engine meant a round could shift the mix and produce a delta unrelated to the scaffold, requiring engine composition to be pinned and logged. **One engine family removes that confound entirely.** Tiering survives as Opus→Haiku, which is cleanly comparable. |
| Cost of it | Work that would have run free on the Mac now spends subscription, across ~77 long runs. **Pushing genuinely mundane steps down to Haiku is what buys that back** — be deliberate rather than defaulting everything to Opus. |
| Scale posture | Owen's Claude subscription, spent opportunistically until exhausted. **No parallel sweep.** |
| Secondary axis | **Owen's daily Claude Code work still works** (see § Gates) |

## The two loops (do not conflate)

1. **Within-project autonomy** — one problem, iterate against the frozen verifier
   until it passes or the cap is hit. Requires no self-modification. Nearly all
   the engineering lives here.
2. **Across-project self-modification** — between projects, rewrite the scaffold.
   The recursive part; nearly all the risk lives here.

**Loop 1 is a hard prerequisite for loop 2.** With no baseline solve rate there
is nothing for self-modification to improve on and no signal telling it what to
change. Claude's standing recommendation: build and measure loop 1 with a frozen
scaffold, establish solve rate and noise floor, *then* unlock self-editing.

## Success Criteria (objectives)

Per `driving-functions.md`, four numbers per round:

1. **Primary — continuous, not binary.** Per task, record where the submission
   lands on the competition's own scale (leaderboard percentile / normalised
   metric). A medal-threshold pass rate is reported *alongside* it as the
   headline number. Rationale: with ~20 tasks and no sweep, a binary solve rate
   has almost no resolution; a continuous score recovers most of the statistical
   power given up when the sweep was dropped.
2. **Marginal round gain** — Δ solve rate per round, in units of seed noise.
3. **Cost per unit gain** — wall-clock and subscription-window consumption per
   point of improvement.
4. **Human-gate load** — interventions per round; must trend to zero.

Targets not yet set — **Open Question Q2**.

## Gates (constraints that must hold, never folded into the objective)

- **Harness integrity** — scorer, verifier, eval data, and measurement skills are
  physically unreachable from the agent's write surface, enforced outside the
  agent's process. A round violating this is void, not adjusted.
- **Cheat / rule-violation detection** — reward hacking is the *expected* shortest
  path for a code-editing agent facing a frozen verifier. The detector must exist
  **before the first self-editing round**; built afterwards, it cannot tell you
  which earlier rounds were real. Cheat rate is a reported number.
- **Secondary axis (Goodhart detector)** — a **fixed, frozen set of ~8–12 canned
  daily tasks** (vault, mail, writing) with pass/fail criteria, run identically
  every round against the evolved scaffold. Frozen before round 0. Must be a
  scored set, not a judgement call, or it detects nothing. The loop never sees
  these, so it cannot optimise them.
  - **Inputs are recorded fixtures**, not live state (Owen, 2026-08-12). A
    snapshot of a vault subset, sample emails, a fixed calendar week, and a
    couple of documents. Most of Owen's daily skills read live state that changes
    daily, so an unfrozen set drifts on its own and produces noise that reads as
    signal — a Goodhart detector that drifts cannot detect Goodhart. Accepted
    cost: the snapshot slowly stops resembling real work.
- **Escalation, not abandonment** (Owen, 2026-08-12). The agent may not quit a
  project it judges hopeless; it escalates to Owen instead. This makes
  **escalations per round** a genuine measure of H4 rather than a tautology —
  under pure mechanical verification human-gate load is zero by construction and
  measures nothing.
  - **Escalation responses must be decisions, not advice.** Fixed vocabulary:
    `continue` / `abandon` / `extend cap`. Free-form guidance ("try gradient
    boosting on that one") makes Owen the improvement mechanism and confounds the
    next round's delta — the `skillify` confound arriving through another door.
- **Rollback** — a self-edit that degrades the scaffold must be revertible.
  Reuses the existing `STAGED → canary → REJECTED → revert → hard_examples`
  pattern; a self-edit is an adapter with a different payload.
- **Per-project cap** — step / token / wall-clock. Replaces the retired dollar
  `BudgetGate` as the runaway-loop brake. "Failed within cap" is a first-class,
  logged outcome; without it, solve rate is undefined.
- **Resumability** — subscription windows close at unpredictable points. An
  interrupted attempt must resume, not restart.
- **Diversity / tail-coverage floors** (`collapse_gate.py`) where still applicable.

## The self-edit step — what the agent may look at

**Aggregate stats + a few sampled trajectories** (Owen, 2026-08-12). At each
round boundary the self-editing agent receives a structured summary — per-task
pass/fail and score, an **error taxonomy**, where time and steps went — plus a
handful of full trajectories.

Rationale: this is the channel through which corpus information enters the
scaffold. Full-trajectory access is the widest channel and the easiest way to
encode task-specific answers into skills; failures-only is blind to what made
successes work; agent-chooses makes the channel vary per round and wrecks
attribution. The chosen option is rich enough to find real patterns and narrow
enough to limit memorisation.

**Consequence — design homework:** the **error taxonomy must exist before round
0** and stay frozen across rounds. "Aggregate stats" are only comparable if
failures are categorised the same way every round.

## Architecture — what gets built vs reused

**Greenfield in the same repo** (Owen, 2026-08-12). Build the solver fresh; lift
only what earns its place.

**Reused on merit:**

- **The safety layer** — shell gate, deny-list, capability tokens,
  `<untrusted_data>` wrapping. *More* important under the new design, not less.
- **The episode store** — becomes trajectory and round logging almost unchanged.
- **The promotion / canary / rollback machinery** — maps onto scaffold versions.

**Left dormant in-tree, not deleted:** coordinator, scheduler, NATS bus, mesh
discovery, worker dispatch, gateway, webui, TUI. All were built to distribute
work across four Jetsons over a LAN; under a Mac-local design there is nothing to
distribute. Keeping them recoverable costs nothing and avoids re-deciding later.

**Consequence — the Jetsons are idle again.** Dropping NATS removes any way to
send them work. **Mac-only for loop 1; Jetsons deferred.** If the sub-step tier
ever outgrows one M4 Pro, a plain HTTP endpoint per Jetson is a tiny fraction of
the NATS + mesh + discovery + signing stack and does not drag the June networking
blocker back onto the critical path. Do not build it until throughput proves the
need.

## The solver — shape of one project attempt

**Single run, iterative refinement against the competition's own validation
split** (Owen, 2026-08-12). Build a solution, score it, revise, repeat until the
cap. Cheapest per project and the honest baseline for round 0.

Two earlier decisions protect this one: continuous scoring means a simple solver
still yields signal rather than a wall of zeros (de-risking the round-0-near-zero
kill condition), and keeping the baseline simple leaves headroom for a genuinely
interesting outcome — **if a self-edit proposes parallel attempts or tree search
on its own, that is a finding**, not a shortcut skipped.

Accepted: per-task variance is higher than a k-parallel or tree-search solver
would give. The noise floor captures it rather than hiding it.

## Corpus split — practice vs held-out

**Score everything; learn from a subset only** (Owen, 2026-08-12). All tasks are
scored and reported. The self-edit summary covers **only the practice subset**;
held-out task results never enter it.

Rationale: without this, the agent reads round *N*'s results on tasks 1–20, edits
its skills, and is re-scored on tasks 1–20 — "got better at ML research" is then
indistinguishable from "memorised twenty Kaggle competitions." The daily-task
axis catches lost generality; it cannot catch within-corpus memorisation, which
is the more likely failure. Costs no measurement power (all tasks still scored),
and a **practice-vs-held-out gap becomes direct evidence of memorisation**.

## The corpus

**Experiment 1: 11 problems — 6 speedup + 5 Kaggle-style — scored separately by
type, never averaged.** Split ~7 practice / ~4 held-out. **Locked before the
noise floor and unchangeable after** — `driving-functions.md` forbids comparing
rounds measured on different eval sets, so "start small and expand" would restart
the trajectory.

Sizing: noise floor (3 seeds) + round 0 + 3 rounds ≈ **7 full passes** ≈ ~77 long
agentic runs on an opportunistic subscription.

**Speedup problems come from Owen's own repos.** Uncontaminated (Owen's code is
in no pretraining set), and timing is mechanical so no scoring script is needed.

### Headroom profiling — results, 2026-08-12

Ran as a 7-agent workflow. Every baseline below was **measured** on the M4 Pro
(≥2 runs each), not estimated. **Verdict: five validated problems, not six.**

| # | Problem | Baseline | Headroom | Notes |
|---|---|---|---|---|
| 1 | Claim-preservation scorer — `evals/research_summarize/scoring.py` | 21.9 s (0.9% spread) | **9.5×** | 16 ONNX embed calls per case where 1 suffices. Bit-identical gate. **Best of the set** |
| 2 | `VaultIndex.query` — `vault/index.py:56-67` | 5.27 s | **25.5×** | 20k separate `np.dot` calls + full sort for k=5 |
| 3 | `DeterministicHashEmbedder.embed` — `vault/embedder.py` | 7.47 s | **7.2×** | No token→vector memoisation. Bit-identical fix. Caveat: test-only embedder |
| 4 | `retry.test.ts` — **Maestro** `packages/core/src/retry.ts` | 507 s | **~500×** | A *bug*: `DEFAULT_TRANSIENT_RETRY_*` never exported from `@maestro/shared` → unbounded spin loop |
| 5 | `EmbeddingModel` config — `memory/embeddings.py:80-91` | 4.33 s (needs scaling to ~3000 texts) | **5.9×** | `intra_op=2` on 14 cores; `enable_padding(128)` on 10-token strings |
| 6 | `SignedTransport` / `ReplayWindow._evict` — `transport/envelope.py:92-96` | **24.5 s @ N=50k** (24.492 / 24.268, 0.9% spread) | **2.0×** | **Confirmed viable 2026-08-12** — see below |

**Problem 6 confirmed at N=50k** (measured directly, not by an agent):

```
N=50000, ttl=60000ms, clock advances 1ms/msg
[run 1] FULL SignedTransport publish+verify+replay:   24.492s (489.8 us/msg) delivered=50000 errs=0
[run 2] FULL SignedTransport publish+verify+replay:   24.268s (485.4 us/msg) delivered=50000 errs=0
[run 1] ReplayWindow shipped _evict:  12.090s | OrderedDict evict: 0.016s | component 778.9x
[run 2] ReplayWindow shipped _evict:  12.355s | OrderedDict evict: 0.016s | component 793.8x
```

**Half the total runtime is one quadratic loop.** `_evict` rebuilds a list
comprehension over the entire `_seen` dict on every `observe()`; with ttl=60s and
the clock advancing 1 ms per message, nothing ever actually expires within
N=50k — so it scans up to 50k entries, 50k times, and deletes nothing. Removing
that saves ~12.2 s of a 24.4 s baseline: **2.0× on the full path**, versus 1.37×
at N=20k. Correctness gate is green and free: `pytest tests/test_transport/ -q` →
**52 passed in 0.07s**, and it pins replay rejection, stale/future timestamps, and
sender binding, so a "fast" solution that stops deduping fails instantly.

*Caveat:* the 0.016 s figure uses an OrderedDict fix that relies on timestamps
being monotonic (insertion order == timestamp order), which holds in this
benchmark but not in general. A generally-correct fix may be somewhat slower, so
treat **2.0× as the ceiling** on the full path rather than a guaranteed target.

**The speedup set is complete at six.** Corpus for experiment 1: **6 speedup +
5 Kaggle = 11 problems.**

**Negative results (genuinely useful — these are disqualified):**

- **webui + TUI: zero candidates.** Both subtrees too small and too fast.
- **NCA training loop** (43.4 s): only real win is `torch.set_num_threads(4)` =
  1.69×, a one-liner. Structural items are 1–2% each. **Effectively already fast.**
- **NCA recovery sweep** (38.3 s): only 1.61×, and its gate is broken by
  construction (below).
- **Vault watcher cold-start**: duplicate of #5, and the surveyed 6.4× did not
  reproduce — real figure 2.0×.
- **Maestro full suite**: 96% of it *is* `retry.test.ts`; use the single file.

**Gate traps verified during profiling — these must go into the harness or the
benchmark silently breaks:**

1. **NCA recovery sweep: the correct fix changes the answer.** Hoisting
   `baseline_mse` removes its consumption of the global torch RNG stream, so
   the output vector changes. Any gate pinning it *fails the intended fix*.
2. **NCA is not bit-exact across processes** (3 identical runs → 3 different
   final losses). Needs ~1e-3 relative tolerance.
3. **`VaultIndex.query` scores drift ~1e-7** after vectorising; top-k paths and
   order were identical across 500 queries. Exact-score pinning would fail every
   legitimate solution.
4. **Maestro: "same pass/fail set" is the wrong gate** — the correct fix makes 3
   currently-failing tests pass. Gate must be "all 21 pass, other 111 files
   unchanged." Baseline is not green regardless (18 pre-existing failures).
5. **Maestro loophole:** an agent could hardcode the constants into `retry.ts`
   instead of exporting them from `@maestro/shared`. Both pass. **Decide whether
   that counts** — this is a reward-hacking decision, not a detail.
6. **Padding changes shift embedding vectors** — #5 must gate on cosine > 0.9999,
   never exact equality.

**Two structural weaknesses in the resulting set:**

- **Domain diversity is thin.** Three of five (#2, #3, #5) are the same
  Turing vault/embedding family. Distinct inefficiencies, so not redundant — but
  the benchmark is narrow.
- **#4 is a bug-fix, not an optimisation.** "Find the missing export" is
  plausibly a different capability from "vectorise this loop," and its 8.5-minute
  baseline makes iteration expensive.

**Sixth problem: resolved** — `SignedTransport` re-measured at N=50k, viable (see
above). Unused fallbacks, kept on record: survey `memory/retriever.py` (the
4-parallel-query perceive path — a classic N+1 shape, untouched by this sweep, and
the obvious source if domain diversity needs widening later) · rebalance toward
Kaggle.

**Kaggle problems: 5, deliberately light.** Owen's instinct was "one simple one
that shouldn't be too much"; revised to 5 because **a type with one problem in it
is not a measurement** — no trend is readable off a single task at any number of
rounds, and a demonstration sitting beside a measurement invites being reported
as one.

**"Real research questions" are deferred to experiment 2** (Owen, 2026-08-12).
They have no built-in scoring, so each needs a scoring script — the slow part,
per problem — and sourcing them is open-ended work sitting on the critical path
before the noise floor. That is the exact shape of the two months this repo went
quiet in June. A separate test set requires a separate trajectory anyway, so
deferring costs nothing; Owen builds that set while loop 1 runs.

Why not Lite as published: **MLE-bench assumes a CUDA box.** On an M4 Pro with
Metal and no ROSIE, tabular and classical-ML competitions run fine; serious
vision/NLP training tasks are impractical or impossible. Taking Lite unfiltered
would load the corpus with tasks the machine cannot complete — scoring as
failures and dragging round 0 toward the near-zero kill condition for reasons
unrelated to the idea.

**Selection guardrail (non-negotiable).** The criterion must be **mechanical,
pre-registered, and about feasibility only** — e.g. "completes end-to-end on this
Mac in under N hours with the baseline solver." **Never expected score, never
'looks promising.'** If tasks enter because the agent does well on them, every
downstream number is invalid and no later rigor recovers it. Write the criterion
before looking at any result; state the deviation from published Lite openly in
the writeup.

## Operating mode

**Fully unattended; escalations are the only interrupt** (Owen, 2026-08-12).
Self-edits apply automatically, the next round starts, Owen finds out in the
morning. Only an escalation or a stopping condition halts the loop.

Rejected alternative and why: approving each self-edit would pin human-gate load
at ≥1 per round permanently, making "escalations trending to zero" unmeasurable —
the same trap as free-form escalation advice, through a different door.

Consequences:

- **Rollback and the cheat detector are load-bearing**, not nice-to-have — they
  are the only thing between a bad self-edit and three unwatched wasted rounds.
  Both are loop-2 prerequisites; one more argument for loop-1-first.
- **Escalation delivery reuses `coordinator/alerts/ntfy_client.py`** — self-hosted
  ntfy, per-operator topic, built as the closed-laptop fallback. An unattended
  loop that must reach Owen at 2am is exactly what it was written for. A fourth
  module surviving the greenfield cut on merit.

## Execution environment

**On the Mac, a fresh working directory per project** — `~/turing-workspace/<project-id>/`
as the agent's cwd and only declared workspace — **owned by a separate `turing`
macOS user** (Owen, 2026-08-12).

Why the separate user rather than the directory alone: Turing's shell gate
constrains commands *Turing* runs, but the agent's job is authoring Python and
executing it, and a Python process writes anywhere its user can write regardless
of cwd. A directory is a convention; a user account is a boundary the OS
enforces. The realistic failure is not malice — it is `rmtree` on an empty path
variable, or a training run filling the disk unattended at 3am. The `turing` user
has no reach into Owen's vault, SSH keys, MCP credentials, or Claude credentials.

## Baseline & Noise Floor

Round 0 is the **frozen-scaffold** agent on the same corpus, measured exactly as
every later round. **The noise floor comes first**: same config, ≥3 seeds, before
any self-editing round. Because budget is opportunistic, the noise floor has
**first claim on it** — measuring it late makes every earlier number
uninterpretable retroactively.

Wrinkle vs. a training loop: the agent is stochastic *within* a project as well
as across seeds; the two sources of spread need separating.

## Stopping Criterion

**Pre-committed 2026-08-12.** Stop when any of:

- Marginal gain < noise floor for **1 round** (saturation → write up).
- The cheat detector fires (round void; investigate before continuing).
- Secondary axis drops below its round-0 level by more than its own noise (Goodhart).
- A constraint gate fails (harness integrity, rollback failure).
- **Round budget R = 3** reached (hard backstop).

Subscription exhaustion **pauses**, it does not stop — state is checkpointed.

Owen chose the tight rule over Claude's proposed 2-round / R=5. Accepted risk,
recorded: a single below-noise round may itself be noise, so this rule will
sometimes call saturation early. Tolerable **only because saturation is a finding
rather than a kill condition** — an early call costs a writeup that can be
resumed, not the project. Under the original all-fatal framing this rule would
have been dangerous.

## Budget

- Dollar metering **retired** at Owen's direction; `BudgetGate`'s $10/day cap and
  billing semantics removed. A step/token/wall-clock cap replaces its safety role.
- Compute is Owen's Claude subscription, spent opportunistically until exhausted.
  Running out is accepted.
- Consequence, accepted: **no parallel sweep**, therefore no cross-config claims.
  Findings are directional and must be reported as such.
- ROSIE array sweep: **deferred**, not cancelled. Seam documented in #379.

## Resources

- **Turing repo** — `~/Developer/active/Turing`, `main`, clean. 10 ADRs;
  bench-cycle proves the flywheel seams software-only.
- **Skills repo** — `~/Developer/active/skills`. Source for the `turing-skills`
  fork; itself read-only to the agent.
- **ROSIE** — `dgx`/`dgxh100` allow 21-day jobs; `dgxh100` is 8×H100/node, only
  one node usable at the 2026-08-12 check. `/home` 97% full — use `/data`.
  GitHub-over-SSH works as `oap22`. **Outbound internet from compute nodes:
  unverified (Q7).**
- **Mac** — **MacBook Pro `Mac16,7`, M4 Pro (10P/4E), 48 GB unified, 273 GB/s**
  (verified 2026-08-12). Ollama, `mlx` 0.32.0 / `mlx-lm` 0.31.3 installed; no LM
  Studio. Only model pulled: `muse-glimmer:30b-mlx` (21 GB) — a **dense** 30B
  (all ~29.6B params active per token), which is *why* it is slow: ~15–20 tok/s
  *(inferred from Meta's M4 Max figure scaled by bandwidth, not measured)*.
  Architecture, not configuration.
- **Recommended sub-step model:**
  `mlx-community/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-4bit` (17.8 GB) —
  **converted with mlx-lm 0.31.3, the exact version already installed.**
  Expected ~80–90 tok/s, **an inference** anchored on a published ~83 tok/s
  measurement of the same architecture family (Nemotron 3 Nano 30B-A3B) on an
  identical M4 Pro / 48 GB. Roughly 4–5× Glimmer.
  - **Why it beats the alternatives for *this* workload:** Mamba-2 layers carry a
    constant-size recurrent state instead of a growing KV cache. Qwen3-Coder-30B-A3B
    — the strongest rival, and the only candidate with a hard tool-reliability
    number (76.9%) — measures 73.6 tok/s at 1K context but **13.5 tok/s at 64K**.
    The sub-step tier parses logs and files; that cliff is the wrong failure mode.
  - **Gotchas:** it is a reasoning model — **disable thinking** for cheap
    sub-steps or hundreds of reasoning tokens get spent reformatting JSON. Tool
    parser is `qwen3_coder`, not plain OpenAI JSON.
  - **Unverified:** no published tokens/sec for this model under MLX on any Apple
    Silicon, and **no function-calling benchmark for it at all**. Both claims are
    architectural inference. **Prerequisite: benchmark it against Glimmer on this
    machine** (`mlx_lm.generate --max-tokens 128`) before the design depends on it.
- **`NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4`** (Q8, resolved 2026-08-12,
  verified against the HF model card). Hybrid Mamba-2 + MoE, **30B total / 3B
  active**, 1M context, ~20T pretraining tokens, explicitly targeted at agent
  systems. NVFP4 runs on **Ampere and Hopper via W4A16 kernels** — not
  Blackwell-only. vLLM / TensorRT-LLM / SGLang / Ollama / llama.cpp supported.
  Ships DSpark, DFlash, and MTP speculative-decoding drafters.
  - **Mac: adopt.** Straight upgrade over Glimmer for the sub-step tier — same
    parameter class, a fraction of the per-token compute (3B active vs 30B
    dense). Owen's "should be faster" intuition is correct.
  - **Jetson: no.** ~16 GB of 4-bit weights against 8 GB unified memory. The
    blocker is memory, not architecture (Orin is Ampere, which the kernels
    support). Jetsons need a genuinely small model — separate selection problem,
    tracked as issue #259.
  - **ROSIE H100s: technically yes** (W4A16), but **excluded** — see below.
- **ROSIE posture (Owen, 2026-08-12).** *"I do not want to run any LLMs on
  ROSIE."* No inference of any kind on the cluster; the H100s are held in reserve
  for experiments that genuinely need them. **All local inference runs on the
  Mac.** Note for the record: under the agreed plan there is no H100 requirement
  at all — no fine-tuning, no sweep, no inference — so ROSIE is effectively out
  of this program. If an H100 need appears, that is a signal the scope changed
  and the brief should be revisited.
- **Jetson fleet ×4** — **sub-step tier** (Q6, resolved 2026-08-12). Owen's
  position: Jetsons can run research agents on smaller models. Claude's recorded
  correction, accepted into the design: a 3–8B model at Q4 in 8 GB asked to
  autonomously write and debug Kaggle-grade ML code will very likely floor — and
  "round-0 solve rate near zero" is a **kill condition**, so putting Jetsons on
  the critical path risks killing the idea on a hardware result. They serve
  mundane sub-steps instead. **Engine size becomes a deliberate sweep axis later**
  ("how small before the scaffold stops compensating?") rather than a drifting
  mixture now.
- **Surface coordinator** — role under the new framing still undefined.
  Deployment stalled on headless multi-node networking since 2026-06-09.
- `research/` scaffolding (`JOURNAL.md`, `OPEN-QUESTIONS.md`, `DEAD-ENDS.md`,
  `results/`) does **not** exist in the Turing repo yet. Prerequisite.

## Non-Goals

- Not building a competitive MLE-bench agent. That leaderboard is crowded; the
  contribution is the self-improvement axis, which nobody on it reports.
- Not making absolute capability claims. Kaggle solutions are in pretraining
  data; contamination is inherited and roughly constant across rounds, which
  preserves *deltas* and invalidates *absolutes*.
- Not the ROSIE parallel sweep (deferred).
- Not local model fine-tuning (demoted; possible later as cost reduction).
- Not "any research question, problem, or goal" in the first program. Claude's
  recorded recommendation: one task family, one corpus, one loop, measured
  honestly. Generality is the aspiration, not the scope.

## Multi-agent workflow boundary

**The self-editing agent is exempt from the PR/review process inside
`turing-skills` only** (Owen, 2026-08-12). It commits freely there. The **Turing
repo keeps the full `CLAUDE.md` process** — issue-assignee claiming, one branch =
one agent, PR with green CI, CODEOWNERS hotspots, mandatory human review tiers.

This matters twice: an autonomous committer would otherwise violate those
conventions by construction, and Joey (the other contributor) would be sharing a
repo with one. It also narrows the action space usefully — self-edits reach
skills, never Turing's source.

## Time & pacing

**No deadline; build it properly** (Owen, 2026-08-12). Recorded risk: this repo
already stalled for two months (last commit 2026-06-09), and a long build with no
measurement checkpoint is how that recurs.

Agreed mitigation — **run a bench cycle for the new architecture first**, reusing
Turing's own idiom: every seam real (project dispatch, solver loop, cap, scoring,
round boundary, self-edit, rollback), edges synthetic (canned solver outputs, stub
verifier, scripted self-edit). Two rounds back-to-back, the second rigged to fail
so the rollback chain is proven before anything real is at stake. Days, not weeks,
and no subscription budget.

## Prerequisites before round 0

**Loop 1 (frozen scaffold, no self-editing):** `research/` scaffolding · the
`turing-skills` fork · MLE-bench harness integrated and runnable locally · the
within-project autonomous solver · per-project cap with checkpoint/resume · the
frozen daily-task fixture set · noise-floor runs at ≥3 seeds.

**Loop 2 (add before the first self-edit):** harness isolation actually enforced ·
the cheat detector · rollback · the frozen error taxonomy.

Note the split: **all four of the riskiest items are loop-2 items.** Nothing is
self-editing during loop 1, so there is no harness to game and nothing to roll
back. A measured round 0 is reachable without building any of them — the
strongest argument for the loop-1-first sequencing.

## Priors & Dead Ends

**Owen has attempted nothing in this direction before — this is the first
attempt** (2026-08-12). `DEAD-ENDS.md` starts empty.

Consequence: **every prior in this brief is borrowed from the literature, none
from Owen's own experience.** Therefore — log **estimate vs actual** (wall-clock,
cost, solve rate) from the very first run, so real priors start accumulating; and
the bench cycle is worth *more*, not less, given no personal intuition for how
this class of loop behaves.


- **ADR 0009's guardrail list** — accumulate-never-replace, retrain-from-base,
  frontier dedup, reasoning-not-style, eval on diversity + tails. Written for a
  weight-training loop; **most do not transfer directly to a code-editing loop**
  and need re-derivation rather than copying.
- **"Expect 1–3 useful rounds then saturation"** — ADR 0009's own prior. A loop
  reporting monotonic gains over ten rounds should be treated as a measurement
  bug until proven otherwise, contamination checked first.
- **ReST-EM regressed on coding after iteration 1.** The closest published
  analogue to a verifier-driven self-improvement loop — a cautionary result, not
  a supporting one.
- **The verifiable retarget quietly swapped ADR 0009's knowledge-injection
  mechanism.** ADR 0009 requires knowledge to enter from outside the student
  model (fetched sources + cloud teacher). Verifier-filtered self-distillation
  puts the verifier in that role instead. Raised in interview; partly moot under
  the code-editing framing, but must be addressed in the superseding ADR.
- **Deployment blocker** — headless multi-node networking / IP config, stalled
  since 2026-06-09. The new framing largely routes around it.

## Open Questions

| # | Question | How it closes |
|---|---|---|
| Q2 | Success targets: what solve-rate delta counts as the idea working? | Design gate, after the noise floor exists |
| Q6b | What is the Surface coordinator for under the new framing? | Owen |
| Q7 | Does ROSIE allow outbound internet from **compute** nodes? | One SSH check. **De-prioritised** — only bites when the deferred sweep is queued |
| Q13 | Which small model runs on the 8 GB Jetsons for the sub-step tier? | Issue #259, still open |
| Q9 | Exact MLE-bench figures (task count, Lite size, per-attempt budget) — quoted from memory in-interview, **unverified** | Read the paper/repo |
| Q10 | Which ADRs are superseded, and the number for the new one | Follows from an agreed brief |
| Q11 | How is harness read-only-ness *enforced* (separate process, container, mount)? | Design gate — **loop-2 blocker** |
| Q12 | Which ~8–12 tasks, and what the recorded fixtures contain | Before round 0 — **loop-1 blocker** |
| Q14 | Measured tok/s: Nemotron 3.5 Lightning MLX-4bit vs muse-glimmer on this M4 Pro | 5-minute `mlx_lm.generate` benchmark |
| Q15 | The frozen error taxonomy for round summaries | Before the first self-edit |

## Interview Log

**2026-08-12, Owen + Claude.** Began as an interview on the ADR-0009 flywheel and
#379's verifiable retarget; became a scoping interview for a **fundamental change
of target**.

Decisions that moved during the interview:

- Opened intending to pin the unit of self-improvement (weights vs scaffold).
  Owen instead redefined the project: Turing takes *any* research goal and works
  autonomously until the result is verifiable.
- Verifier provenance settled early and cleanly (human-supplied, frozen) — the
  single most load-bearing choice; it removes the reward-hacking hole at the
  criterion level, though not at the harness level.
- Corpus moved from "author our own" to "inherit an existing benchmark harness"
  after Claude showed the human bottleneck had merely *relocated* from per-round
  curation to per-project verifier authoring.
- Claude flagged that #379 silently swaps ADR 0009's knowledge-injection
  mechanism for verifier-filtered self-distillation (STaR/ReST-EM), landing on
  the weakest ground in Owen's own evidence base.
- Owen chose the most aggressive edit surface (whole repo bar harness), accepting
  rollback as load-bearing.
- Owen retired dollar budgeting; Claude substituted a step/token/wall-clock cap
  to preserve the runaway-loop brake.
- Owen accepted losing the parallel sweep — and with it statistical power — in
  exchange for running opportunistically on subscription. Claude derived the
  consequent requirement that attempts be checkpointed and resumable.
- "Skills ARE the scaffold" created a direct contradiction with the edit-surface
  decision, since the measurement layer lives in the skills repo, and a second
  problem: Owen's own `skillify` edits would confound the trajectory. Resolved by
  forking a dedicated `turing-skills` repo.
- Owen initially marked all four failure modes fatal, including saturation at
  round 1–2. Claude flagged this as a pre-commitment to abandon on Owen's own
  predicted outcome; Owen reversed it to "saturation is the finding".
- Secondary axis: Owen asked for an explanation of the Goodhart mechanism before
  choosing, then selected the daily-Claude-Code-work measure — apt because the
  scaffold *is* skills, so the test directly probes whether corpus gains were
  bought by destroying generality.
