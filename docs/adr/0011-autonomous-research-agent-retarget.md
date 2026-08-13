# ADR 0011 — Retarget to an autonomous research agent: frozen `(goal, verifier)` projects, skills-as-scaffold, loop 1 before loop 2

- Status: Proposed
- Date: 2026-08-12
- Relates to: ADR 0009 (Jetson fleet + research flywheel), ADR 0010 (Discord
  retirement, ntfy alert fallback), ADR 0007 (adapter promotion canary), ADR 0008
  (NATS presence), ADR 0003 (capability tokens), issue #379, `CONTEXT.md`.
  **Supersedes** ADR 0009's Phase 0 reward-signal model ("nightly batch + morning
  human curation", §2) and its specialty strategy ("one shared AI/ML-generalist
  adapter, replicated ×4", §3), together with the Jetson-worker premise both rest
  on. **Amends** ADR 0009 §4 (the fine-tuning guardrails survive as background
  knowledge requiring re-derivation, not as active rules). **Demotes** ADR 0010's
  webui-as-primary-operator-surface decision to dormant, while keeping its ntfy
  alert transport.
- Source of record: `research/briefs/2026-08-12-autonomous-research-agent.md`
  (status `agreed`). **That brief, not this ADR, is the authority on rationale**;
  this ADR records the decisions and their consequences for the codebase. Where
  the brief left something open, this ADR says so rather than closing it.

## Context

ADR 0009 set the target as *fine-tune four Jetsons into AI/ML mini-experts* via a
human-curated data flywheel. On 2026-08-12 an interview that opened on pinning
the unit of self-improvement (weights vs scaffold) instead produced a fundamental
change of target: **Turing becomes a general research agent** that, given a
problem, works autonomously until the result passes a human-supplied verifier,
and that **edits its own scaffold** between projects to get better at doing so.

The pivot is recorded first because it invalidates prior documents. What it kills
or demotes:

- **ADR 0009's specialty strategy, generalist adapter, Jetson-worker premise, and
  human-curation reward loop.** All four served the old target. Hence this ADR.
- **Issue #379** shrinks from "the retarget" to one possible task family.
- **The local LoRA flywheel** has no job under this framing unless re-chartered
  as *cost reduction* (distilling scaffold behaviour onto local models).
  Downstream, not now.
- **`BudgetGate`'s dollar metering** — retired at the operator's direction.

What survives on merit: the episode store, the safety model (shell gate,
capability tokens, Ed25519 signing, `<untrusted_data>` wrapping), the
promotion/canary rollback machinery, the ntfy alert client, and ADR 0009's
guardrail list *as background knowledge*.

The research question this architecture exists to answer: **does an agent that
rewrites its own scaffold between projects improve its solve rate on a held-out
corpus of verifier-bearing ML projects — by how much per round, at what cost, and
where does it stop?**

## Decision

### 1. The unit of work is a `(goal, verifier)` pair, and the verifier is frozen

A **project** is a goal plus a verifier. The verifier is **human-supplied, or
agent-proposed and human-approved before work begins** — frozen from that moment
either way. **The agent may never edit, relax, or regenerate the bar it is graded
on.** The freezing is the load-bearing property, not the authorship.

This costs nothing per round: the corpus is locked before the noise floor, so
every approval happens once, at setup.

The agent **builds its own validation splits, diagnostics, and internal measures
freely** — that is how it navigates a problem at all. **None of them may ever
count as the official score.** *The agent invents; the operator holds the ruler.*

Codified in `src/turing/research/contracts.py`: `Verifier` is a frozen dataclass,
`__init_subclass__` rejects a subclass that declares `__setattr__` or
`__delattr__` in its own body, and `Problem` behaviourally probes its verifier at
construction. Any code path that lets a solver mutate a verifier is a **critical
defect**, not a bug.

**What that does and does not buy, stated plainly** — because this is the
invariant the whole program rests on and an overclaim here is worse than no
claim. Both checks are one-shot: the subclass check fires at class-creation
time, the probe at `Problem` construction. Neither survives a class attribute
assigned *afterwards*. Assigning `SomeVerifier.__setattr__ = <function that
raises only for the probe's attribute name>` from solver-side code defeats both,
and re-binding the mutated verifier into a fresh `Problem` is then accepted
silently. `object.__setattr__` inside the same process defeats them too, which
`contracts.py`'s own docstring says.

So: these mechanisms make **accidental** mutation impossible and **deliberate**
mutation conspicuous. They are not a sandbox. **The only real enforcement is
process isolation** — keeping the harness, scorer, eval data and the verifier
definitions physically unreachable from the agent's write surface. That is brief
Q11, it is open, and §6 below records the write-surface question it depends on.
Loop 1 does not self-edit, so nothing is running against these checks yet; they
must be hardened before the first self-edit, and §17 lists the specific gaps.

### 2. Two loops. Loop 1 ships and is measured first

1. **Within-project autonomy** — one problem, iterate against the frozen verifier
   until it passes or the cap trips. Requires no self-modification. Nearly all
   the engineering lives here.
2. **Across-project self-modification** — between projects, rewrite the scaffold.
   The recursive part; nearly all the risk lives here.

**Loop 1 is a hard prerequisite for loop 2.** With no baseline solve rate there
is nothing for self-modification to improve on and no signal telling it what to
change. Build and measure loop 1 with a frozen scaffold, establish solve rate and
noise floor, *then* unlock self-editing.

The sequencing argument that settles it: the riskiest prerequisites — **enforced
harness isolation, the cheat detector, and rollback** — are *all* loop-2 items.
Nothing self-edits during loop 1, so there is no harness to game and nothing to
roll back. **A measured round 0 is reachable without building any of them.**

Loop-1 prerequisites before round 0: `research/` scaffolding · the `turing-skills`
fork · MLE-bench harness integrated and runnable locally · the within-project
autonomous solver · per-project cap with checkpoint/resume · the frozen
daily-task fixture set · noise-floor runs at ≥3 seeds · **the frozen error
taxonomy** (see immediately below) · **the escalation channel reachable in
practice** (§4).

Loop-2 prerequisites before the first self-edit: harness isolation actually
enforced · the cheat detector · rollback · the contract hardening in §17 · the
write-surface decision in §6.

**The error taxonomy moved, and the move is the point.** The brief states its
gate twice and differently: its Prerequisites list files it under *"Loop 2 (add
before the first self-edit)"*, while § The self-edit step calls it out as
**design homework — "the error taxonomy must exist before round 0 and stay frozen
across rounds"** — on the reasoning that *"aggregate stats are only comparable if
failures are categorised the same way every round."*

Those are different gates, and the looser one silently defeats the stricter one's
purpose: if round 0's failures are categorised ad hoc and the taxonomy is only
authored before round 1, **round 0's aggregate stats are not comparable to round
1's**, which is the exact defect the design-homework note exists to prevent.
Round 0 is a round; it produces the failures the first self-edit reads. This ADR
therefore adopts the **stricter** reading — taxonomy before round 0 — as the
conservative default, and records the brief's internal disagreement as **R4**
rather than pretending it was never there. Authoring it early costs a design
session; authoring it late costs round 0's comparability, which cannot be
recovered without re-running round 0.

### 3. Scoring is continuous and per-type, and is never averaged across types

Four numbers per round, per `driving-functions.md`:

1. **Primary — continuous, not binary.** Per task, where the submission lands on
   the problem's own scale (leaderboard percentile, normalised metric, speedup
   ratio). A medal-threshold pass rate is reported *alongside* it. With ~11 tasks
   and no parallel sweep, a binary solve rate has almost no resolution; a
   continuous score recovers most of the statistical power given up when the
   sweep was dropped. *(The brief says "~20 tasks" in Success Criteria and 11 in
   § The corpus. 11 is used here because it is the number the corpus section
   actually enumerates — 6 speedup + 5 Kaggle — and the argument is stronger, not
   weaker, at the smaller count. The discrepancy is the brief's, noted rather
   than silently reconciled.)*
2. **Marginal round gain** — Δ per round, in units of seed noise.
3. **Cost per unit gain** — wall-clock and subscription-window consumption per
   point of improvement.
4. **Human-gate load** — escalations per round; must trend to zero.

**Score each problem type separately; never average across types.** A round that
helps one type and hurts another otherwise reads as flat. `RoundRecord` therefore
carries a tuple of per-`(type, split)` `TypeScore` cells and deliberately exposes
**no aggregate score attribute**; a test asserts none is ever added.

Targets are **not set** — brief Open Question Q2, closing at a design gate after
the noise floor exists.

### 4. The agent escalates; it does not abandon

**The agent may not quit a project it judges hopeless.** It escalates to the
operator instead. This is what makes escalations-per-round a genuine measure of
H4: under pure mechanical verification, human-gate load is zero by construction
and measures nothing.

**Escalation responses are decisions, not advice.** Fixed vocabulary:
`continue` / `abandon` / `extend_cap`. Free-form guidance ("try gradient boosting
on that one") makes the operator the improvement mechanism and confounds the next
round's delta — the `skillify` confound arriving through another door.

Consequences in code, at their real strength:

- `EscalationDecision` is three-valued with **no free-text field**, it is
  `slots=True`, and `decode_decision` rejects unknown keys loudly rather than
  ignoring them. This one *is* structural: there is no shape advice can arrive
  in. A test asserts no free-text field is ever added.
- **There is no self-quit transition anywhere in the solver**, and every writer
  of `AttemptState.ABANDONED` gates on an `EscalationVerdict.ABANDON`. But that
  guarantee lives in the call sites (`solver.py`, `runner.py`), not in
  `Attempt`: `Attempt.evolve` forwards arbitrary `**changes` to
  `dataclasses.replace`, so `evolve(state=ABANDONED)` constructs a terminal
  abandoned attempt with no escalation behind it. **The invariant is a
  convention two call sites honour, not one the type prevents breaking.** Given
  §1 calls verifier mutation a critical defect, the same standard applies here:
  moving the check into `Attempt` is tracked in §17.

Escalation delivery reuses `coordinator/alerts/ntfy_client.py` (ADR 0010 §2) —
self-hosted ntfy, per-operator topic, built as the closed-laptop fallback. An
unattended loop that must reach the operator at 2am is exactly what it was
written for.

**That transport is not available today, and the loop must not be run
unattended until it is.** Three facts compose badly and are recorded here so
they are not rediscovered at 2am:

1. `ResearchLoopSettings.operator_ntfy_topic` and `coordinator_ntfy_base_url`
   both default to `None`, and `NtfyAlertClient` treats either being `None` as a
   **silent no-op**. Unconfigured, the push does not fail — it does nothing.
2. The ntfy server is self-hosted **on the Surface coordinator**, which §7 below
   lists as dormant and whose deployment has been stalled on headless
   multi-node networking since 2026-06-09 (brief Q6b). The escalation channel
   therefore depends on a machine this same ADR declares out of service.
3. Escalation is the agent's **only** exit. A dropped push does not degrade to a
   worse outcome; it suspends the loop indefinitely waiting for a decision that
   nobody knows to write.

The inbound half does not depend on ntfy: decisions arrive as a file drop
(`<request_id>.decision.json` in the round's `escalations/` directory), written
by `python -m turing.research.loop.cli`. So the honest operating posture until
the transport exists is **poll the inbox with `--list`**, not "wait to be
paged". `docs/research-agent.md` § Escalations carries the commands.

### 5. A step / token / wall-clock cap replaces dollar metering, and attempts are resumable

`BudgetGate`'s $10/day cap and billing semantics are **retired**. Compute is the
operator's Claude subscription, spent opportunistically until exhausted. A
**per-project step / token / wall-clock cap** takes over its safety role as the
runaway-loop brake. **"Failed within cap" is a first-class, logged outcome** —
without it, solve rate is undefined.

Subscription windows close at unpredictable points, so **an interrupted attempt
must resume, not restart**. Every attempt is an immutable checkpoint record;
persisting the latest one and reloading it is the whole of resume. An
interruption costs the remainder of the attempt, not the attempt.

`Cap.extend` is the **sanctioned** path: it only ever grows a cap, refuses a
negative or empty extension, and increments a lineage count so "this project got
three extensions" is visible in the record. It is reached only from an operator
`extend_cap` decision.

It is not, however, the **only** path, and the difference matters because the cap
is the runaway-loop brake that replaced dollar metering. `Attempt.evolve`
forwards arbitrary `**changes` to `dataclasses.replace`, so `cap` and `consumed`
are both freely replaceable: `evolve(cap=Cap(max_steps=1, …))` swaps the cap
wholesale with `extension_count` still reading 0, and
`evolve(consumed=CapConsumption())` resets the meter to zero. Either would let a
runaway attempt run past its brake while the round record reports that no
extension was granted. **Closing that — making `cap` and `consumed`
non-replaceable through `evolve` — is a loop-2 prerequisite tracked in §17**, and
it is more urgent than it looks, because §6's write-surface question decides
whether a self-edit can reach this code at all.

### 6. The scaffold is skills, in a dedicated `turing-skills` repo forked at a frozen commit

**Self-edits land as skill files.** A git SHA is the scaffold version per round,
`git revert` is rollback, and a diff is the answer to "what changed" — the
improving artifact is code, and git already has the machinery.

The scaffold lives in a **dedicated `turing-skills` repo, forked from
`~/Developer/active/skills` at a frozen commit**, for two independent reasons:

- **The measurement layer lives in the skills repo.** `research-loop`,
  `driving-functions.md`, and `log_run.py` are the ruler. An agent with write
  access to its own ruler is not measured.
- **The operator's own `skillify` edits would confound the trajectory.** Daily
  edits to a shared skills repo would land inside the scaffold between rounds and
  show up as scaffold improvement.

Divergence from the personal skills repo is the accepted cost.
`scripts/bootstrap-turing-skills.sh` creates the fork locally, records the source
SHA and commit date in `FORK-PROVENANCE.md`, tags the fork point, removes the
inherited `origin` remote, and **`git rm -r`s `skills/research-loop` out of the
fork**. No remote is created by that script.

That deletion is a decision, not a detail, and it is recorded here rather than
left as an implementer's inference. The brief makes the measurement layer
(`research-loop/`: `SKILL.md`, `conventions.md`, `driving-functions.md`,
`log_run.py`) **read-only to the agent**. Marking it read-only *inside a repo the
agent commits freely to* is a convention with nothing enforcing it, so the fork
does not contain it at all; the canonical copy stays in the source repo, outside
the agent's write surface. Two loose ends this leaves, recorded so they are not
found later as surprises:

- The fork's `manifest.json` still lists a `research-loop` entry pointing at the
  removed directory. `install.py` skips a missing skill with a warning, so this
  is noise rather than breakage — but `bootstrap-turing-skills.sh --verify`
  checks only the directory, not the manifest, so the stale entry is invisible to
  the check that exists.
- The fork also inherits `install.py`, which symlinks skills into `~/.claude`,
  `~/.codex` and the vault based on `$HOME`. Run from the fork **as the
  operator**, it would silently repoint the operator's daily harness at the
  agent's editable fork — which is also the Goodhart secondary axis. Nothing in
  the script neutralises it. Under §8's sandbox user this cannot happen; today,
  §8 is provisioned and not yet used, so it can.

#### Editable surface — the brief holds two readings and this ADR does not pick one silently

The brief states both, one page apart, and they are not compatible:

- *Settled decisions, "Editable surface"* — "Whole Turing repo + `turing-skills`
  writable; harness, verifier, scorer, eval data, and measurement skills in
  separate read-only repos." The interview log agrees: "Owen chose the most
  aggressive edit surface (whole repo bar harness)."
- *§ Multi-agent workflow boundary* — "self-edits reach skills, **never Turing's
  source**", which is also what keeps an autonomous committer from violating
  `CLAUDE.md`'s PR process by construction.

The wide reading is not merely broader; it **voids three invariants this same ADR
records**, because the Turing repo contains all three of their implementations:

| Invariant | Where it lives in this repo | What a writable repo does to it |
|---|---|---|
| §1 the frozen verifier | `src/turing/research/contracts.py` | a self-edit can relax the enforcement, or the bar itself |
| §5 the cap | `contracts.py` `Cap` / `CapConsumption` | a self-edit can under-count its own consumption |
| §11 held-out isolation | `research/results/` (the settings default) | held-out `TypeScore` cells are readable straight off disk |

> **Amendment, 2026-08-13 — the §11 row's premise no longer holds.**
> `ResearchLoopSettings.research_results_root` now defaults to
> `~/research-results/` rather than the repo-relative `research/results/`, so
> that Turing Desktop can watch one root while research code lives in any
> project. The row is left as written because it records the state at decision
> time, but two consequences follow and neither is closed here:
> **(a)** the §11 hazard no longer rides on repo write access, so widening the
> write surface to the Turing repo costs one fewer invariant than the table
> claims; **(b)** conversely, narrowing the repo surface no longer protects the
> held-out cells at all — they now sit in the operator's home directory,
> reachable by anything running as the operator. §11 needs its own boundary
> (Q11's mechanism question, again), not the repo's. §6's position below is
> otherwise unchanged: the write surface remains `turing-skills` only.

**Position taken here, and it is deliberately the narrow one:** for the purposes
of this ADR the agent's write surface is **`turing-skills` only**. That costs
loop 1 nothing — nothing self-edits during loop 1, so no capability is given up
today — and it is the reading under which §1, §5 and §11 remain true statements.

**This does not close the question.** Widening the surface to the Turing repo is
an operator decision that must be taken explicitly, before the first self-edit,
together with the mechanism that would keep the three rows above enforceable
under it (Q11 again: a separate process, a container, or a mount is what makes
"read-only" mean something). Recorded in the open-questions table below as
**R1** (`R` = raised after the brief, so brief Q-numbers stay a faithful
mirror of the brief's own table). Until it closes, "read-only" is a convention and this ADR does not claim
otherwise.

**Multi-agent workflow boundary.** The self-editing agent is exempt from the
PR/review process **inside `turing-skills` only**; it commits freely there. The
Turing repo keeps the full `CLAUDE.md` process — issue-assignee claiming, one
branch = one agent, PR with green CI, CODEOWNERS hotspots, mandatory human review
tiers. This matters twice: an autonomous committer would otherwise violate those
conventions by construction, and the second contributor would be sharing a repo
with one. It also narrows the action space usefully — **self-edits reach skills,
never Turing's source.**

### 7. Greenfield in the same repo, with selective reuse; the distributed stack goes dormant

Build the solver fresh, under `src/turing/research/`. Lift only what earns its
place.

**Reused on merit — four modules:**

| Module | Why it survives |
|---|---|
| **The safety layer** — shell gate, deny-list, capability tokens, `<untrusted_data>` wrapping | *More* important under the new design, not less: the agent's job is authoring and executing code |
| **The episode store** | Becomes trajectory and round logging almost unchanged |
| **The promotion / canary / rollback machinery** (ADR 0007) | Maps onto scaffold versions — a self-edit is an adapter with a different payload |
| **`coordinator/alerts/ntfy_client.py`** (ADR 0010 §2) | The closed-laptop escalation channel for an unattended loop. **The client survives on merit; the server it posts to does not yet exist** — it is self-hosted on the coordinator this same section lists as dormant. See §4 and R3 |

**Left dormant in-tree, not deleted:** coordinator, scheduler, NATS bus (ADR
0008), mesh discovery, worker dispatch, gateway, webui, TUI. All were built to
distribute work across four Jetsons over a LAN; under a Mac-local design there is
nothing to distribute. **Keeping them recoverable costs nothing and avoids
re-deciding later.** Dormant means: not edited, not deleted, not on the critical
path. Their tests keep running.

### 8. Execution environment: a fresh working directory per project, owned by a separate `turing` macOS user

The agent's cwd and only declared workspace is `~/turing-workspace/<project-id>/`,
**owned by a separate `turing` macOS user**.

Why the separate user rather than the directory alone: Turing's shell gate
constrains commands *Turing* runs, but the agent's job is authoring Python and
executing it, and **a Python process writes anywhere its user can write
regardless of cwd. A directory is a convention; a user account is a boundary the
OS enforces.** The realistic failure is not malice — it is `rmtree` on an empty
path variable, or a training run filling the disk unattended at 3am. The `turing`
user has no reach into the operator's vault, SSH keys, MCP credentials, or Claude
credentials.

Provisioning: `scripts/setup-research-sandbox.sh`, which prints its plan and
requires an explicit confirmation flag before touching anything.

**Provisioned is not the same as used, and today it is only provisioned.** Two
gaps sit between the script and the guarantee above, both recorded as **R2**:

1. `ResearchLoopSettings.research_workspace_root` defaults to
   `Path.home() / "turing-workspace"`. Run as the operator, that resolves inside
   the *operator's* home. `TURING_RESEARCH_WORKSPACE_ROOT` must be set to
   `/Users/turing/turing-workspace` or the sandbox is a directory nothing writes
   into.
2. **No code under `src/turing/research/` drops privileges.** Setting the env var
   is necessary and not sufficient: a 700 turing-owned workspace is not writable
   by an operator-owned process, and one that could write there would still be
   running as the operator. The loop has to be launched under the sandbox user
   and that wiring does not exist.

`--verify` asserts both rather than assuming them, and says "the sandbox is
sealed" rather than "the agent is inside it". Until R2 closes, **§8 describes an
intended boundary, not an enforced one** — which also means §1's "process
isolation is the only real enforcement" has nothing standing behind it yet.

**Tightening the operator home hides the scaffold from the agent.** Step 5 puts
the operator home at mode 750 and the sandbox user is deliberately not in
`staff`, so a process running as `turing` cannot traverse it — including
`~/Developer/active/turing-skills`, the default destination of
`bootstrap-turing-skills.sh`. Following the two scripts with their defaults
produces a setup where either the boundary holds and the agent cannot reach its
own writable scaffold, or the loop runs as the operator and the boundary is
decoration. The sandbox script therefore provisions a shared exchange directory
(`/Users/Shared/turing`, setgid to the `turing` group with the operator added to
it) and the fork is created there via `TURING_SKILLS_DEST`. The operator joining
the `turing` group does not widen the sandbox's reach: operator files are group
`staff`, which `turing` is not in.

### 9. The Jetsons go idle, and that is acceptable

Dropping NATS removes any way to send the Jetsons work. **Mac-only for loop 1;
Jetsons deferred.**

Why acceptable rather than a loss:

- Putting them on the critical path was already rejected on its own merits. A
  3–8B model at Q4 in 8 GB, asked to autonomously write and debug Kaggle-grade ML
  code, will very likely floor — and **"round-0 solve rate near zero" is a kill
  condition**, so a Jetson-hosted round 0 risks killing the idea on a hardware
  result.
- The agreed engine is **Claude only, on the operator's subscription** (Opus
  orchestrates; Haiku-tier handles genuinely mundane sub-steps), with **no local
  model in the loop**. One engine family removes a whole class of confound: a
  split Claude/local engine meant a round could shift the mix and produce a delta
  unrelated to the scaffold. Tiering survives as Opus→Haiku, which is cleanly
  comparable.
- The recommended sub-step model for the Mac is
  `mlx-community/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-4bit`; the Jetsons cannot
  host it (≈16 GB of 4-bit weights against 8 GB unified). Their model selection
  is a separate problem, tracked as issue #259 (brief Q13).
- **The recovery path is cheap.** If the sub-step tier ever outgrows one M4 Pro,
  a plain HTTP endpoint per Jetson is a tiny fraction of the NATS + mesh +
  discovery + signing stack, and does not drag the June networking blocker back
  onto the critical path. **Do not build it until throughput proves the need.**

Cost of the Claude-only engine, accepted: work that would have run free on the
Mac now spends subscription across ~77 long runs. **Pushing genuinely mundane
steps down to Haiku is what buys that back** — be deliberate rather than
defaulting everything to Opus.

### 10. A local-model seam, not a local-model implementation

Build **one backend interface** — send messages plus tools, get a response — with
a Claude implementation today and a local one droppable in later as backup.
**Not implemented now.**

### 11. Corpus, split, and what round 0 is measured against

- **Experiment 1: 11 problems — 6 speedup + 5 Kaggle-style.** Scored separately
  by type, never averaged. Split roughly 7 practice / 4 held-out.
- **Locked before the noise floor and unchangeable after.** `driving-functions.md`
  forbids comparing rounds measured on different eval sets, so "start small and
  expand" would restart the trajectory.
- **Score everything; learn from a subset only.** All tasks are scored and
  reported; the self-edit summary covers **only the practice subset**. Without
  this, the agent reads round *N*'s results on tasks 1–11, edits its skills, and
  is re-scored on tasks 1–11 — "got better at ML research" becomes
  indistinguishable from "memorised eleven competitions". A **practice-vs-held-out
  gap is direct evidence of memorisation**, and it costs no measurement power.
- **Speedup problems come from the operator's own repos** — uncontaminated, and
  timing is mechanical so no scoring script is needed.
- **Selection guardrail (non-negotiable).** The criterion for admitting a task
  must be **mechanical, pre-registered, and about feasibility only** — never
  expected score, never "looks promising". If tasks enter because the agent does
  well on them, every downstream number is invalid and no later rigor recovers
  it.
- **Round 0 is the frozen-scaffold agent on the same corpus, measured exactly as
  every later round. The noise floor comes first** — same config, ≥3 seeds,
  before any self-editing round — and it has **first claim on an opportunistic
  budget**, because measuring it late makes every earlier number uninterpretable
  retroactively.

The solver shape for round 0 is a **single run with iterative refinement against
the problem's own validation split**: build, score, revise, until the cap. It is
the cheapest per project and the honest baseline. **If a self-edit later proposes
parallel attempts or tree search on its own, that is a finding**, not a shortcut
that was skipped.

### 12. What of ADR 0009 survives, and the knowledge-injection swap

**Superseded outright:**

- **§2 Phase 0 operating model.** Nightly batch + morning human curation, the
  human-gated question queue, and "these human decisions are the Phase 0 reward
  signal" are replaced by **mechanical verification against a frozen verifier**.
  The operator's remaining decision surface is the three-word escalation
  vocabulary of §4 above — and the design target is that it trends to zero.
- **§3 Specialty strategy.** One shared AI/ML-generalist adapter replicated ×4
  presupposes Jetson workers being fine-tuned. Nothing is fine-tuned under this
  ADR.

**Amended, not discarded:** §4's fine-tuning guardrail table
(accumulate-never-replace, retrain-from-base, frontier dedup,
reasoning-not-style, eval on diversity + tails). It was written for a
weight-training loop. **Most of it does not transfer directly to a code-editing
loop and needs re-derivation rather than copying.** Two lines transfer as
priors and are adopted as such:

- *"Expect 1–3 useful rounds, then re-evaluate — not perpetual gains."* A loop
  reporting monotonic gains over ten rounds is a measurement bug until proven
  otherwise; check contamination first.
- **ReST-EM regressed on coding after iteration 1.** The closest published
  analogue to a verifier-driven self-improvement loop is a *cautionary* result,
  not a supporting one.

**The knowledge-injection swap, addressed as the brief requires.** ADR 0009 §4's
first and most load-bearing rule is that **knowledge must enter from outside the
student model** — fetched real sources and/or a stronger teacher — because
recursive self-distillation collapses. The verifiable retarget in #379 quietly
replaced that mechanism with verifier-filtered self-distillation (STaR/ReST-EM
shaped), putting the verifier in the teacher's seat. The position taken here:

- **Under a code-editing framing the collapse literature does not transfer
  directly.** No weights are trained; the engine is a fixed external Claude whose
  capability does not degrade from being used. "Model collapse" has no referent
  when the model is not the improving artifact.
- **The analogous risk is real but different**: the scaffold's self-edits are
  derived only from the scaffold's own trajectories on a fixed corpus, so the
  only information entering the scaffold is **corpus information**. That is not
  collapse; it is **memorisation**, and its detector is the practice/held-out gap
  of §11 plus the frozen secondary axis of §13.
- **What is genuinely unresolved:** whether a frozen verifier is a sufficient
  external signal to drive improvement at all, as opposed to merely a sufficient
  filter to prevent regression. That is hypothesis H2, and it is what the
  experiment measures. This ADR records the swap and its residual risk; it does
  not claim to have retired it.

### 13. Gates ride alongside the objective, never inside it

Constraints stay pass/fail and are never folded into the primary scalar:
**harness integrity** (a violating round is void, not adjusted) · **cheat /
rule-violation detection**, which must exist *before the first self-editing
round* because a detector built afterwards cannot tell you which earlier rounds
were real · a **frozen secondary axis** of ~8–12 canned daily tasks run
identically every round against the evolved scaffold, with **recorded fixtures
rather than live state** (a Goodhart detector that drifts cannot detect Goodhart)
· **rollback** · the **per-project cap** · **resumability** · diversity /
tail-coverage floors where still applicable.

### 14. Operating mode: fully unattended; escalations are the only interrupt

Self-edits apply automatically, the next round starts, the operator finds out in
the morning. Only an escalation or a stopping condition halts the loop.

Rejected alternative: approving each self-edit would pin human-gate load at ≥1
per round permanently, making "escalations trending to zero" unmeasurable — the
same trap as free-form escalation advice, through a different door.

This is precisely why **rollback and the cheat detector are load-bearing rather
than nice-to-have**: they are the only thing between a bad self-edit and three
unwatched wasted rounds. One more argument for loop-1-first.

### 15. Stopping criterion, pre-committed

Stop when any of: marginal gain < noise floor for **1 round** (saturation → write
up) · the cheat detector fires (round void; investigate) · the secondary axis
drops below its round-0 level by more than its own noise (Goodhart) · a
constraint gate fails · **round budget R = 3** reached.

**Subscription exhaustion pauses; it does not stop.** State is checkpointed.

Accepted risk, recorded: the tight rule (1 round, R=3) was chosen over a proposed
2-round / R=5 rule. A single below-noise round may itself be noise, so this will
sometimes call saturation early. **Tolerable only because saturation is a finding
rather than a kill condition** — an early call costs a writeup that can be
resumed, not the project.

**A second interaction, not noticed when the rule was pre-committed.** Hypothesis
H2's falsifier is *"Δ within noise for the **first two** self-editing rounds"*,
but this rule stops the loop after **one** below-noise round. If round 1 — the
first self-editing round — lands below the noise floor, the loop stops and round
2 never runs, so H2 can be neither confirmed nor refuted by its own criterion on
what the brief calls the most likely outcome. Both numbers originate in the
brief; this ADR does not silently pick one. Recorded as **R5**, to be settled
before the first self-editing round — the cheap resolution is to restate H2's
falsifier at one round, or to carve an explicit exception letting round 2 run
when round 1 is the only below-noise round.

### 16. What the self-editing agent may look at — the information channel

Loop 2 is out of scope for implementation, but this is a **settled brief
decision** and it is the decision most directly governing memorisation risk, so
it belongs in the decision record rather than being re-derived later from
`self_edit_seam.py`.

At each round boundary the self-editing agent receives **aggregate stats plus a
few sampled trajectories**: per-task pass/fail and score, an error taxonomy
(§2's before-round-0 item), where time and steps went, and a handful of full
trajectories. **Practice subset only** — held-out results never enter it (§11).

This is *the* channel through which corpus information enters the scaffold, which
is why its width is a decision and not an implementation detail. The rejected
alternatives, with the reason each was rejected:

| Rejected | Why |
|---|---|
| Full-trajectory access | The widest channel, and the easiest way to encode task-specific answers into skills |
| Failures only | Blind to what made successes work |
| Agent chooses what to see | The channel varies per round, which wrecks attribution: a delta then mixes scaffold change with channel change |

The chosen option is rich enough to find real patterns and narrow enough to limit
memorisation. Its detector is the practice-vs-held-out gap of §11 plus the frozen
secondary axis of §13 — the channel bounds the risk, those two measure whether
the bound held.

### 17. Contract hardening required before the first self-edit

The types in `contracts.py` encode §1, §4 and §5 well enough for loop 1, where
nothing self-edits and every writer is code in this repo under review. They are
**not** sufficient once a self-edit can add a call site. Listed here so the
first-self-edit gate has something concrete to check, and so §1's, §4's and §5's
qualifications are not left as prose:

| Gap | Today | Needed |
|---|---|---|
| A verifier can be made writable after class creation (`Verifier.__setattr__` assigned post-hoc), and a mutated verifier re-binds into a fresh `Problem` silently | one-shot checks at class creation and at `Problem` construction | re-probe at bind time against the *live* class, or move verification out of the solver's process entirely (Q11) |
| `Attempt.evolve(state=ABANDONED)` reaches a terminal abandoned attempt with no escalation behind it | the two real writers gate on an operator `abandon` verdict | `Attempt` itself refuses `ABANDONED` without an `escalation_id` |
| `Attempt.evolve(cap=…)` swaps the cap wholesale, `extension_count` unchanged | `Cap.extend` is the sanctioned path, used everywhere | `evolve` refuses `cap` outright; growth only via `apply_cap_extension` |
| `Attempt.evolve(consumed=CapConsumption())` resets the meter | `record_consumption` only ever adds | `evolve` refuses `consumed`; or `consumed` is monotonic by construction |

None of these is reachable by the agent during loop 1 — they are reachable by
*code*, and during loop 1 all the code is written under the repo's review
process. That is exactly the property §6's write-surface decision (R1) either
preserves or destroys.

## Consequences

- **Round 0 becomes reachable without the four riskiest components.** That is the
  point of the sequencing and the main reason this ADR is worth filing before the
  loop-2 design exists.
- **The distributed stack stops being maintained but keeps costing CI time.**
  Dormant modules still have tests. Accepted: they are green today, and deleting
  them would forfeit the recovery path §9 relies on.
- **`CONTEXT.md` is now wrong in several places** — node assignments, the
  self-improvement phases, and the operator surface all describe the ADR
  0009/0010 world. Updating it is a CODEOWNERS-hotspot change requiring human
  review, and is deliberately left to a separate PR rather than bundled here.
- **The webui and the Surface coordinator lose their defined role.** ADR 0010
  made the webui the single primary operator surface for directing work; under an
  unattended loop whose only interrupt is a three-word escalation over ntfy,
  there is very little work to direct. The Surface coordinator's role is
  explicitly **open** (brief Q6b).
- **The `turing` macOS user must exist before the first real run**, and creating
  it changes the operator's own home-directory permissions. That is an
  irreversible-ish local change and is why the setup script refuses to act
  without an explicit flag. **It also partitions the machine**: after it, the
  operator home and the sandbox home cannot see each other, so anything both
  sides need — the scaffold fork above all — has to live in the shared exchange
  directory. Setup is no longer "run two scripts with defaults"; the order and
  the two env vars matter, and `docs/research-agent.md` § One-time machine setup
  is the authority on both.
- **The escalation channel is a loop-1 blocker, which it was not expected to
  be.** Escalation is the agent's only exit and its outbound transport is
  unconfigured by default and hosted on a dormant machine (§4, R3). An unattended
  round started before R3 closes does not fail loudly — it suspends silently.
- **Two repos now hold Turing-relevant scaffold**, with different rules: Turing
  under full PR process, `turing-skills` under none. Anyone reading a
  `turing-skills` commit log should not expect review to have happened.
- **The corpus, once locked, cannot be extended.** Any addition restarts the
  trajectory. Sizing is 6 speedup + 5 Kaggle, and the accepted cost is thin
  per-family signal — roughly 3 problems per type in a 10–12 corpus.
- **No parallel sweep, therefore no cross-config claims.** Findings are
  directional and must be reported as such.
- **Contamination is inherited and roughly constant across rounds.** Kaggle
  solutions are in pretraining data. This preserves *deltas* and invalidates
  *absolutes*; no absolute capability claim may be made from this corpus.
- **Novelty is unmeasured by design.** Nothing prevents the agent from pushing
  boundaries — the verifier never asks which technique was used — and nothing
  rewards it either: a metric-maximiser takes the cheapest path, normally a
  strong known method applied well. Measuring novelty mechanically is unsolved,
  not deferred. **Not a success criterion; do not read it into results after the
  fact.**
- **Recorded pacing risk.** This repo already stalled for two months (last commit
  before the current run of work: 2026-06-09), and a long build with no
  measurement checkpoint is how that recurs. Agreed mitigation: **run a bench
  cycle for the new architecture first** — every seam real (project dispatch,
  solver loop, cap, scoring, round boundary, self-edit, rollback), edges
  synthetic (canned solver outputs, stub verifier, scripted self-edit), two rounds
  back-to-back with the second rigged to fail so the rollback chain is proven
  before anything real is at stake. Days, not weeks, and no subscription budget.

## Open questions carried by this ADR

Closing these is not a precondition for filing it; each names its gate. `Q`
numbers are the brief's own and are never reused; `R` numbers are questions
**raised after the brief** — by this ADR or by review of it — so the two sets
cannot collide.

| # | Question | Gate |
|---|---|---|
| Q2 | What solve-rate delta counts as the idea working? | Design gate, after the noise floor |
| Q6b | What is the Surface coordinator for under the new framing? | Operator. Now also blocks R3 — it hosts the ntfy server |
| Q11 | How is harness read-only-ness *enforced*? | Design gate — **loop-2 blocker** |
| Q12 | Which ~8–12 daily tasks, and what the fixtures contain | Before round 0 — **loop-1 blocker** |
| Q15 | The frozen error taxonomy for round summaries | **Before round 0** — see §2 and R4 |
| R1 | Is the agent's write surface `turing-skills` only, or the whole Turing repo? §6 takes the narrow reading and does not close it | Before the first self-edit — **and it gates §17** |
| R2 | Wiring the loop into the §8 sandbox: workspace root, and the privilege drop that does not exist | Before the first unattended run — **loop-1 blocker** |
| R3 | The escalation channel is unconfigured by default and self-hosted on a dormant machine (§4) | Before the first unattended run — **loop-1 blocker** |
| R4 | The brief gates the frozen error taxonomy at both "before round 0" and "before the first self-edit". §2 adopts the stricter reading | Before round 0 |
| R5 | H2's falsifier needs two below-noise self-editing rounds; §15's stopping rule can stop after one | Before the first self-editing round |

Full list, including the de-prioritised ones: `research/OPEN-QUESTIONS.md`.

## Out of scope

- **Loop 2 in its entirety** — scaffold self-modification, the cheat detector,
  rollback wiring, and enforced harness isolation. Seams are left where loop 2
  attaches; nothing in loop 1 calls them. *Decisions about loop 2 are still
  recorded here (§16's information channel, §17's contract hardening, §6's write
  surface): only the implementation is out of scope, because a decision record
  that omits settled decisions forces a future implementer to re-derive them.*
- The ROSIE parallel sweep (deferred, not cancelled). Under the agreed plan there
  is **no H100 requirement at all** — no fine-tuning, no sweep, no inference. If
  an H100 need appears, that is a signal the scope changed and the brief should
  be revisited.
- Local model fine-tuning (demoted; possible later as cost reduction).
- "Real research questions" as a problem type — deferred to experiment 2. Each
  needs a scoring script, and sourcing them is open-ended work that would sit on
  the critical path before the noise floor.
- Building a competitive MLE-bench agent. That leaderboard is crowded; the
  contribution is the self-improvement axis, which nobody on it reports.
- Deleting the dormant coordinator/NATS/mesh/gateway/worker code.

## References

- **`research/briefs/2026-08-12-autonomous-research-agent.md`** — the source of
  record for every decision above, including the interview log showing which ones
  moved and why.
- `~/Developer/active/skills/skills/research-loop/driving-functions.md` — the four
  numbers, the noise-floor-first rule, the gate-vs-objective distinction, and the
  round-discipline layout.
- `~/Developer/active/skills/skills/research-loop/conventions.md` — the on-disk
  spec for `research/`.
- ADR 0009 — superseded in §2 and §3, amended in §4; its evidence base remains the
  literature backing for the saturation prior.
- ADR 0010 — ntfy alert transport retained (§2); webui-as-primary-surface demoted.
- ADR 0007 — promotion / canary / rollback machinery, reused for scaffold
  versions.
- `src/turing/research/contracts.py` — the frozen-verifier, cap, attempt,
  escalation, and round-record types that encode §1, §3, §4, and §5. §17 lists
  where they stop short of what §1, §4 and §5 assert.
- `src/turing/research/loop/escalation.py` and `loop/cli.py` — the outbound ntfy
  push, the decision-file inbox, and the operator's entire command surface.
- `scripts/setup-research-sandbox.sh`, `scripts/bootstrap-turing-skills.sh` —
  §8's sandbox and §6's fork. Run in that order, and read
  `docs/research-agent.md` § One-time machine setup first: the defaults conflict.
- `docs/research-agent.md` — the operator guide these decisions produce.
- Issue #379 — the verifiable retarget, now one possible task family rather than
  the retarget itself.
