# Turing as an autonomous research agent — operator guide

How to run a round, how to answer an escalation, where the trajectory lands, what
the four numbers mean, and what is deliberately not built yet.

- **Source of record:** `research/briefs/2026-08-12-autonomous-research-agent.md`.
  Where this guide and the brief disagree, the brief wins.
- **Decision record:** `docs/adr/0011-autonomous-research-agent-retarget.md`.
- **Measurement spec:** `~/Developer/active/skills/skills/research-loop/driving-functions.md`.
- **On-disk spec for `research/`:** `conventions.md`, same directory.

> **Status, honestly.** Loop 1 is under construction. The contracts, the solver,
> the backends, the round runner and the escalation CLI are written and their
> tests pass; **no round has ever been run**, and three things stand between this
> repo and a first unattended round: the sandbox is provisioned but the loop does
> not run inside it (ADR 0011 R2), the escalation transport is unconfigured and
> self-hosted on a dormant machine (R3), and the secondary-axis fixture set does
> not exist (brief Q12). Each is called out where it bites.
>
> Sections that name a command you cannot run yet are marked **[not wired]**;
> sections describing a guarantee the code does not yet enforce are marked
> **[not enforced]**. Nothing marked either way should be read as a claim.

---

## The one thing that must never happen

A project is a **`(goal, verifier)` pair**, and **the verifier is frozen** from
the moment it is supplied or approved. The agent may never edit, relax, or
regenerate the bar it is graded on.

The agent builds its own validation splits, diagnostics, and internal measures
freely — that is how it navigates a problem at all. None of them ever count as
the official score.

> **The agent invents; you hold the ruler.**

If you ever find yourself editing a verifier because the agent is not passing it,
stop: that ends the trajectory. Every round measured before the edit and every
round after it are measured on different eval sets, and
`driving-functions.md` forbids comparing them. Amending a verifier is not a
tweak — it is a restart.

There is code behind this, and it is worth knowing exactly how much. `Verifier`
is a frozen dataclass; a subclass that declares `__setattr__` or `__delattr__` in
its own body fails at class-creation time; `Problem` behaviourally probes its
verifier when it is constructed. Any code path that lets a solver mutate a
verifier is a **critical defect**, not a bug.

**[not enforced]** Both of those checks are one-shot, and neither survives an
attribute assigned onto the class *afterwards* — `SomeVerifier.__setattr__ = …`
from solver-side code defeats both, and the mutated verifier then re-binds into a
fresh `Problem` without complaint. So the code makes accidental mutation
impossible and deliberate mutation conspicuous; **it is not a sandbox.** The real
boundary is process isolation, which is open (brief Q11) and which the `turing`
user account is only the filesystem half of. ADR 0011 §17 lists what has to
change before the first self-edit.

Practically, for you: **you are the enforcement right now.** Nothing self-edits
during loop 1, so the only hand that can reach a verifier is a human one, which
is why the paragraph above matters more than the code does.

---

## Before round 0 — the prerequisites

Round 0 is not "the first time you run it". It is a measured baseline, and it is
only interpretable if all of this is in place first. From the brief's
§ Prerequisites:

| Prerequisite | State |
|---|---|
| `research/` scaffolding (`JOURNAL.md`, `OPEN-QUESTIONS.md`, `DEAD-ENDS.md`, `results/`) | **done** |
| The `turing` sandbox user | `scripts/setup-research-sandbox.sh` — run it **first** |
| The `turing-skills` fork | `scripts/bootstrap-turing-skills.sh` — run it **second, with `TURING_SKILLS_DEST` set** (see below) |
| The loop actually running inside the sandbox | **outstanding — ADR 0011 R2, loop-1 blocker.** The account exists; nothing points the loop at it and nothing drops privileges to it |
| The escalation channel reachable in practice | **outstanding — ADR 0011 R3, loop-1 blocker.** Escalation is the agent's only exit and its transport is unconfigured by default |
| MLE-bench harness integrated and runnable locally | outstanding |
| The within-project autonomous solver | written, tests pass, never run for real |
| Per-project cap with checkpoint/resume | written, tests pass, never run for real |
| The frozen error taxonomy | **outstanding — brief Q15 / ADR 0011 R4.** Gated *before round 0*, not before the first self-edit: round 0's failures must be categorised the same way round 1's are, or the two rounds are not comparable |
| The frozen daily-task fixture set (secondary axis) | **outstanding — brief Q12, loop-1 blocker** |
| Noise-floor runs at ≥3 seeds | outstanding |

Two ordering rules that are easy to get wrong and expensive to fix:

1. **The noise floor comes first**, before any self-editing round, at ≥3 seeds on
   the same config. The budget is opportunistic, so it has *first claim* on it.
   Measuring it late makes every earlier number uninterpretable retroactively.
2. **The corpus is locked before the noise floor and cannot change afterwards.**
   6 speedup + 5 Kaggle-style, split roughly 7 practice / 4 held-out. "Start
   small and expand" restarts the trajectory.

And one that is easy to get wrong and impossible to fix: **the criterion for
admitting a task to the corpus must be mechanical, pre-registered, and about
feasibility only** — "completes end-to-end on this Mac in under N hours with the
baseline solver", never expected score, never "looks promising". If tasks enter
because the agent does well on them, every downstream number is invalid and no
later rigour recovers it. Write the criterion down before looking at any result.

### One-time machine setup

> **The order matters and the defaults conflict.** The sandbox script tightens
> your home directory to mode 750 and deliberately keeps `turing` out of `staff`,
> so after it runs, **nothing under your home is reachable by the agent** —
> including `~/Developer/active/turing-skills`, which is where the bootstrap
> script puts the fork by default. Run them in the order below, with the env vars
> below, or you get a machine where either the boundary holds and the agent
> cannot reach its own scaffold, or the loop runs as you and the boundary is
> decoration.

```bash
# 1. The sandbox user that owns /Users/turing/turing-workspace, plus the shared
#    exchange directory the fork has to live in. Plan first; it changes nothing
#    until you pass --apply.
bash scripts/setup-research-sandbox.sh
bash scripts/setup-research-sandbox.sh --apply

# 2. Point the loop at the sandbox workspace. Without this the loop writes to
#    *your* ~/turing-workspace and step 1 protected nothing.
echo 'TURING_RESEARCH_WORKSPACE_ROOT=/Users/turing/turing-workspace' >> .env

# 3. The scaffold fork — frozen at a commit, local only, no remote created —
#    in the exchange directory, NOT under your home.
export TURING_SKILLS_DEST=/Users/Shared/turing/turing-skills
bash scripts/bootstrap-turing-skills.sh
bash scripts/bootstrap-turing-skills.sh --apply
bash scripts/bootstrap-turing-skills.sh --verify
sudo chown -R turing:turing "$TURING_SKILLS_DEST"   # it cloned as you
sudo chmod -R g+w "$TURING_SKILLS_DEST"

# 4. The escalation transport (see § Escalations — it is the agent's only exit).
echo 'TURING_OPERATOR_NTFY_TOPIC=<your topic>' >> .env
echo 'TURING_COORDINATOR_NTFY_BASE_URL=http://surface.<tailnet>.ts.net:8090' >> .env

# 5. Assert all of it. Not just "the user exists" — that the loop is pointed at
#    the sandbox, that no unconfined workspace was left in your home, and that
#    the fork is reachable from inside the boundary.
bash scripts/setup-research-sandbox.sh --verify
```

Both scripts are plan-by-default: with no flags they print exactly what they
would do and exit. Read the plan before passing `--apply`.

Why the separate macOS user rather than just a directory: the agent's job is
authoring Python and executing it, and a Python process writes anywhere its user
can write regardless of cwd. **A directory is a convention; a user account is a
boundary the OS enforces.** The failure being defended against is not malice —
it is `rmtree` on an empty path variable, or a run filling the disk unattended at
3am. The second of those is *not* solved; see the appendix the script prints.

**[not enforced] — read this before trusting a green `--verify`.** The first
failure is *not* solved either, yet. `--verify` asserts that the sandbox is
**sealed**; it cannot assert that the agent is **inside** it, because nothing in
`src/turing/research/` drops privileges to `turing`. Setting
`TURING_RESEARCH_WORKSPACE_ROOT` is necessary and not sufficient: that workspace
is mode 700 and turing-owned, so an operator-run loop cannot write there at all,
and one that could would still be running as you — with full reach into the
vault, `.ssh` and `.claude`. Until ADR 0011 **R2** closes, the sandbox is a
provisioned boundary the loop does not go through, and an unattended run is a run
by you, as you. `--verify` says so in its own output rather than printing
"boundary intact" over it.

---

## Running a round

**[not wired]** — `RoundRunner` is written and tested, but there is **no command
that starts a round**: the only operator entry point that ships is the escalation
CLI below. What follows is the shape the contracts fix, so that the
operator-facing behaviour is not a surprise when it lands.

A round is: every problem in the corpus, one attempt each, scored, with the
results written into a run directory. Round 0 uses the frozen scaffold and no
self-editing.

```
noise floor (≥3 seeds)  →  round 0 (frozen scaffold)  →  [loop 2: rounds 1..3]
```

Each attempt is: build a solution, score it against the problem's own validation
split, revise, repeat, until the verifier passes or the cap trips. Single run,
iterative refinement — deliberately the cheapest honest baseline. **If a later
self-edit proposes parallel attempts or tree search on its own, that is a
finding**, not a shortcut that was skipped.

### What stops an attempt

Exactly three things, and "the agent decided to give up" is not among them:

- **The verifier passes.** Terminal state `PASSED`.
- **The cap trips** — steps, tokens, or wall-clock. Terminal state
  `FAILED_WITHIN_CAP`. This is a **first-class, logged outcome**; without it,
  solve rate is undefined.
- **The agent escalates to you.** State `ESCALATED`, which is neither terminal
  nor resumable: only your decision moves it.

There is no self-quit transition anywhere in the solver. That is deliberate —
see § Escalations.

### Interruptions

Subscription windows close at unpredictable points. **Subscription exhaustion
pauses a run; it does not stop it.** Every attempt is an immutable checkpoint:
persisting the latest record and reloading it *is* the whole of resume. An
interruption costs the remainder of the attempt, not the attempt.

If a run dies mid-round, resume it rather than restarting it. Restarting a round
throws away real measurement and, worse, costs subscription you have already
spent.

---

## Escalations — how to answer

The agent **may not quit a project it judges hopeless.** It escalates to you.
This is not politeness: under pure mechanical verification, human-gate load is
zero by construction and therefore measures nothing. Making escalation the only
exit makes **escalations per round** a genuine measurement of hypothesis H4.

Under the agreed operating mode — fully unattended, self-edits apply
automatically — **an escalation is the only interrupt.**

### How one reaches you, and how you answer

Two separate channels, and only one of them works out of the box.

**Outward — a best-effort ntfy push.** `coordinator/alerts/ntfy_client.py` (ADR
0010 §2) POSTs the request to your per-operator topic and re-pushes on an
interval for as long as the loop waits.

> **[not enforced] This channel is silent until you configure it, and its server
> is not deployed.** `TURING_OPERATOR_NTFY_TOPIC` and
> `TURING_COORDINATOR_NTFY_BASE_URL` both default to unset, and the client treats
> either being unset as a **no-op — it does not fail, it does nothing**. The
> server is self-hosted on the Surface coordinator, whose deployment has been
> stalled since 2026-06-09 and which ADR 0011 §7 lists as dormant. So: an
> unattended round started today escalates into silence, and because the agent
> may not quit, it then waits indefinitely. That is ADR 0011 **R3**, and it is a
> loop-1 blocker. Until it closes, **poll the inbox; do not wait to be paged.**

**Inward — a decision file, written by the CLI.** This half does not depend on
ntfy and works today. Escalations land as `<request_id>.request.json` in the
round's `escalations/` directory; your reply lands beside it as
`<request_id>.decision.json`. A file drop rather than a socket, so a decision
written while the loop is down is picked up when it comes back.

```bash
# What is waiting. This is the polling command until R3 closes.
python -m turing.research.loop.cli \
    --loop-dir ~/research-results/loop-<slug>/round-00/escalations --list

# Answer one. The verdict is one of exactly three words.
python -m turing.research.loop.cli \
    --loop-dir ~/research-results/loop-<slug>/round-00/escalations <request_id> continue

python -m turing.research.loop.cli \
    --loop-dir ~/research-results/loop-<slug>/round-00/escalations <request_id> abandon

python -m turing.research.loop.cli \
    --loop-dir ~/research-results/loop-<slug>/round-00/escalations <request_id> extend_cap \
    --extra-steps 200 --extra-tokens 500000 --extra-wall-clock 3600
```

There is no `--note` and there will never be one; see § Do not send advice. You
can hand-write the decision file if you prefer, but the decoder rejects unknown
keys, so the CLI is the only thing that saves you from finding out which keys
those are.

### The vocabulary is three words. Use only those three.

| Verdict | Meaning | Effect |
|---|---|---|
| `continue` | Keep working under the existing cap | Cap unchanged |
| `abandon` | Stop this project | Attempt goes to `ABANDONED` |
| `extend_cap` | Keep working, with more budget | Requires a numeric extension: extra steps / tokens / wall-clock |

Rules the CLI enforces, so **you** cannot get them subtly wrong:

- `extend_cap` **must** carry a numeric extension; `continue` and `abandon` must
  **not**. A malformed reply is rejected, not guessed at.
- `Cap.extend` only ever grows a cap, never shrinks it, and each extension
  increments a lineage count, so "this project got three extensions" is visible
  in the record.
- Every writer of `ABANDONED` in the solver and the runner gates on your
  `abandon` verdict. There is no self-quit transition anywhere in the solver.

**[not enforced]** The last two are guarantees about the *call sites*, not about
the types. `Attempt.evolve` forwards arbitrary field replacement, so a future
edit that calls `evolve(state=ABANDONED)` or `evolve(consumed=CapConsumption())`
would bypass both without failing anything. Nothing does that today and all
current code is under the repo's review process — but it is a convention, not a
wall, and ADR 0011 §17 tracks moving it into `Attempt` before the first
self-edit. If you are reviewing a PR that touches the solver, this is the thing
to look for.

### Do not send advice

> **This is the rule most likely to be broken by accident, and it silently
> destroys the next round's number.**

"Try gradient boosting on that one" is not an escalation response. Free-form
guidance makes *you* the improvement mechanism: the next round's delta then
measures your intervention mixed with the scaffold's self-edit, and the two
cannot be separated afterwards. It is the `skillify` confound arriving through
another door.

`EscalationDecision` therefore has **no free-text field**, and a test asserts none
is ever added. If you find yourself wanting to explain, the honest options are:
`continue` (you think it can still get there), `extend_cap` (you think it needs
more room), or `abandon` (you think it cannot). Write your reasoning in
`JOURNAL.md`, where it is dated and outside the loop, not into the loop.

### What the escalation tells you

The outward channel is rich even though the inward one is three-valued. An
escalation request carries the reason (`CAP_EXHAUSTED`, `NO_VIABLE_APPROACH`,
`HARNESS_FAILURE`, `VERIFIER_UNRUNNABLE`, `REPEATED_REGRESSION`), a summary, the
cap and what was consumed against it, and the best verification result so far.

`HARNESS_FAILURE` and `VERIFIER_UNRUNNABLE` are worth separating from the rest
when you read them: those are problems with *your* measurement apparatus, not
with the agent's attempt, and answering `continue` to a broken verifier just
burns subscription.

---

## Where things land

The written record — journal, questions, dead ends, briefs — stays in the repo.
The *results* do not: they default to `~/research-results/`, outside any repo, so
a loop driven from any project writes where Turing Desktop watches for live
metrics and plots. `TURING_RESEARCH_RESULTS_ROOT` repoints the root.

```
research/                         (in the repo)
  JOURNAL.md            the thread — newest entry on top, never edited in place
  OPEN-QUESTIONS.md     what is not known yet; answered ones stay, checked
  DEAD-ENDS.md          what has been ruled out, one line each — currently empty
  briefs/               the agreed brief; the source of record

~/research-results/               (outside the repo; the desktop's watched root)
  <YYYY-MM-DD-slug>/          a one-off run
    run.json  config.yaml  metrics.json  stdout.log  notes.md  artifacts/
  loop-<slug>/                the self-improvement trajectory
    trajectory.json
    noise-floor/  round-00/  round-01/  …
      escalations/            <id>.request.json  ← the agent asked
                              <id>.decision.json ← you answered (via the CLI)
```

`…/round-NN/escalations` is the directory you pass to `--loop-dir`. Note that
the agent's own working directory is **not** here: it is
`/Users/turing/turing-workspace/<project-id>/`, outside the repo, owned by
another user (§ One-time machine setup). What lands under `~/research-results/`
is the record; what lands in the workspace is the work.

Rules that keep a long trajectory honest:

- **Run IDs are `YYYY-MM-DD-<slug>`**, the slug describing the *question* not the
  tool. The ID is the primary key and never changes once written.
- **`run.json` is written by `log_run.py`; never hand-edit it.** A run with a
  non-zero exit code is logged — failures are data — but is **never cited as a
  result**.
- **Every round records its parent**, and every round records the **eval set
  hash**. Silent eval drift produces beautiful fake curves. `RoundRecord` refuses
  to construct a round > 0 without a parent, and comparing two rounds with
  different eval-set hashes raises rather than returning a number.
- **A round that fails a constraint gate is logged, not deleted.** It is usually
  the most informative round in the sweep.
- **Commit the record; never commit the artifacts.** Now that the results root is
  `~/research-results/` — outside any repo — nothing under it is committed by
  default. Copy the small diffable files (`run.json`, `config.yaml`,
  `metrics.json`, `notes.md`, `trajectory.json`) into the repo when a result is
  worth citing; leave checkpoints, plots and large outputs where they landed.
  `research/results/.gitignore` still encodes which is which.

Journal entries for negative results are written with the same care as positive
ones, and additionally get a line in `DEAD-ENDS.md`. That is the highest-value
habit in the whole protocol: nobody publishes what did not work, so everybody
re-runs it.

**Log estimate vs actual in every entry** — wall-clock, subscription
consumption, solve rate. `DEAD-ENDS.md` is empty because nothing in this
direction has been attempted before, which means every prior in the brief is
borrowed from the literature rather than from experience. Estimate-vs-actual from
the first run is how real priors start accumulating.

---

## The four numbers

Per `driving-functions.md`, every round reports all four. The primary alone
cannot answer "how far does this go".

### 1. Primary score — continuous, per type, never averaged across types

Where the submission lands on the problem's **own** scale: leaderboard
percentile, normalised metric, speedup ratio. A medal-threshold pass rate is
reported *alongside* it, not instead of it.

Why continuous rather than binary: with ~11 tasks and no parallel sweep, a binary
solve rate has almost no resolution. A continuous score recovers most of the
statistical power given up when the sweep was dropped — and it de-risks the
"round-0 solve rate near zero" kill condition, because a partial result still
yields signal instead of a wall of zeros.

**Speedup scores and Kaggle scores are never averaged together.** They are
different scales measuring different capabilities, and a round that helps one and
hurts the other would read as flat. `RoundRecord` has **no aggregate score
attribute**, and a test asserts none is ever added. Read `primary_scores()`,
which gives you one number per `(problem type, split)` cell.

You will also read **practice vs held-out separately**. All tasks are scored; the
self-edit summary sees only the practice subset. **A practice-vs-held-out gap is
direct evidence of memorisation** — the agent getting better at *these eleven
problems* rather than at ML research. Watch that gap more closely than the
headline.

### 2. Marginal round gain — Δ per round, in units of seed noise

The shape of the trajectory. **Saturation is when Δ drops below the seed-noise
floor**, which is why the noise floor must exist before Δ means anything at all.

Expect saturation early. ADR 0009's own evidence base predicts 1–3 useful rounds
(ReST-EM 2312.06585, Self-Rewarding LMs 2401.10020). **Saturation is a finding,
not a failure** — the deliverable in that case is where it saturates, why, and
what moves that point.

A loop reporting monotonic gains over ten rounds should be treated as a
measurement bug until proven otherwise. Check contamination first.

### 3. Cost per unit gain

Wall-clock and subscription-window consumption per point of improvement. This is
the denominator in "how far can I push this", and it usually degrades faster than
the primary improves. That crossover is often the real finding.

There is **no dollar figure**. Dollar metering was retired; `RoundCost` carries
wall-clock, tokens, and attempt count, and a test asserts no dollars field is
added back.

### 4. Human-gate load — escalations per round

Count every decision you had to make for the round to complete. **If this number
rises, the loop is not self-improving — it is a treadmill with extra steps.**
Trending to zero is frequently the headline result for a self-improvement claim,
and it is the number most likely to go unmeasured because nobody thinks to count
their own labour.

This is also why self-edits apply automatically rather than being approved one by
one: approving each edit would pin human-gate load at ≥1 per round permanently,
making "escalations trending to zero" unmeasurable by construction.

### Gates ride alongside, never inside

Diversity floors, tail coverage, harness integrity, and cheat detection stay
**pass/fail**. Do not fold them into the primary as a weighted sum — a blended
score hides which term moved, and "the number went up" stops meaning anything.

### When to stop — pre-committed, do not renegotiate mid-run

Stop when **any** of:

- Marginal gain < noise floor for **1 round** (saturation → write up)
- The cheat detector fires (round void; investigate before continuing)
- The secondary axis drops below its round-0 level by more than its own noise
  (Goodhart)
- A constraint gate fails (harness integrity, rollback failure)
- **Round budget R = 3** reached (hard backstop)

Recorded risk, accepted when this was chosen over a looser 2-round / R=5 rule: a
single below-noise round may itself be noise, so this will sometimes call
saturation early. That is tolerable **only** because saturation is a finding
rather than a kill condition — an early call costs a writeup that can be resumed,
not the project.

**One thing to settle before round 1, not after.** Hypothesis H2 is refuted by
"Δ within noise for the **first two** self-editing rounds", and this rule stops
the loop after **one**. If round 1 lands below the noise floor — the brief's own
most-likely outcome — round 2 never runs and H2 can be neither confirmed nor
refuted by its own falsifier. Both numbers are the brief's; the interaction was
not noticed when the rule was pre-committed. Registered as **R5** in
`OPEN-QUESTIONS.md`. Settle it *before* round 1: changing a pre-committed
stopping rule after seeing the round it would have stopped is not settling it.

---

## What is deliberately NOT built yet

This is loop 1. **Loop 2 — the self-editing loop — is out of scope**, and so is
everything that exists only to make loop 2 safe. The seams are left where loop 2
attaches; nothing in loop 1 calls them.

| Not built | Why it is not built yet | What it blocks |
|---|---|---|
| **Scaffold self-modification (loop 2)** | With no baseline solve rate there is nothing for self-modification to improve on and no signal telling it what to change | Everything after round 0 |
| **The cheat detector** | Reward hacking is the *expected* shortest path for a code-editing agent facing a frozen verifier — but nothing self-edits during loop 1, so there is nothing to game yet | Must exist **before the first self-editing round**. Built afterwards, it cannot tell you which earlier rounds were real |
| **Rollback wiring** | A self-edit that degrades the scaffold must be revertible. There are no self-edits yet. The machinery it will reuse (`STAGED → canary → REJECTED → revert`) already exists from ADR 0007 | The first self-editing round |
| **Enforced harness isolation** | *How* read-only-ness is enforced — separate process, container, mount — is **open (brief Q11)**. The `turing` user account is the filesystem half of the answer, and it is **provisioned but not used**: nothing points the loop at it and nothing drops privileges to it (ADR 0011 R2) | The first self-editing round. A round that violates harness integrity is **void, not adjusted** |
| **A settled write surface** | The brief says both "whole Turing repo + `turing-skills` writable" and "self-edits reach skills, never Turing's source". The wide reading puts the verifier enforcement, the cap accounting, and the held-out score cells inside the agent's write surface, so ADR 0011 §6 takes the narrow one and leaves the widening as an explicit decision (**R1**) | The first self-edit |
| **The frozen secondary-axis fixture set** | Which ~8–12 daily tasks, and what the recorded fixtures contain, is **open (brief Q12)** | **Round 0** — a loop-1 blocker, unlike the rest of this table |

**The frozen error taxonomy is *not* in that table, and moving it out was
deliberate.** The brief gates it twice and differently — its Prerequisites list
says "before the first self-edit", its § The self-edit step calls it design
homework that "must exist **before round 0** and stay frozen across rounds". The
looser gate defeats the stricter one's purpose: round 0 is a round, it produces
the failures the first self-edit reads, and if its failures were categorised ad
hoc then round 0 and round 1 are not comparable — which is the exact defect the
design-homework note exists to prevent, and it cannot be fixed after the fact
without re-running round 0. **Treat it as a before-round-0 item** (ADR 0011 §2
and R4), and it now sits in the prerequisites table at the top of this guide
alongside Q12.

Two of those deserve emphasis, because the operating mode makes them
load-bearing rather than nice-to-have. Self-edits apply automatically and the
next round starts without you: **rollback and the cheat detector are the only
thing standing between a bad self-edit and three unwatched wasted rounds.** That
is one more argument for measuring loop 1 first.

### Also not built, and further out

- **The local-model backend.** One backend interface is built — send messages
  plus tools, get a response — with a Claude implementation today and a local one
  droppable in later. The local implementation is **not** written. The engine is
  Claude only: Opus orchestrates, Haiku-tier handles genuinely mundane sub-steps.
  One engine family removes a whole class of confound, since a shifting
  Claude/local mix would produce round-over-round deltas unrelated to the
  scaffold.
- **Anything on the Jetsons.** Dropping NATS removed any way to send them work.
  Mac-only for loop 1. If the sub-step tier ever outgrows one M4 Pro, a plain
  HTTP endpoint per Jetson is the recovery path — **do not build it until
  throughput proves the need.**
- **The ROSIE parallel sweep.** Deferred, not cancelled. Under the agreed plan
  there is no H100 requirement at all. If an H100 need appears, that is a signal
  the scope changed and the brief should be revisited.
- **"Real research questions" as a problem type.** Deferred to experiment 2 —
  they have no built-in scoring, so each needs a scoring script, and sourcing them
  is open-ended work that would sit on the critical path before the noise floor.
- **Local model fine-tuning.** Demoted. Possible later, re-chartered as cost
  reduction rather than capability.

### Dormant, not deleted

Coordinator, scheduler, NATS bus, mesh discovery, worker dispatch, gateway,
webui, TUI. All were built to distribute work across four Jetsons over a LAN, and
under a Mac-local design there is nothing to distribute. They are **not edited,
not deleted, and not on the critical path** — keeping them recoverable costs
nothing and avoids re-deciding later. If you are working in this repo, treat them
as read-only.

---

## Claims this program cannot make

Worth reading before writing anything up, because these are easy to slide into.

- **No absolute capability claims.** Kaggle solutions are in pretraining data.
  Contamination is inherited and roughly constant across rounds, which preserves
  *deltas* and invalidates *absolutes*.
- **No cross-config claims.** There is no parallel sweep, so findings are
  directional and must be reported as such. One trajectory is an anecdote.
- **No novelty claims.** Nothing prevents the agent from pushing boundaries — the
  verifier never asks which technique was used — and nothing rewards it either: a
  metric-maximiser takes the cheapest path, normally a strong known method
  applied well. Measuring novelty mechanically is unsolved, not deferred. It is
  an aspiration, explicitly not a success criterion. **Do not read it into
  results after the fact.**
- **Not a competitive MLE-bench result.** That leaderboard is crowded; the
  contribution is the self-improvement axis, which nobody on it reports. The
  corpus also deviates from published Lite on purpose — MLE-bench assumes a CUDA
  box, and taking Lite unfiltered on an M4 Pro would load the corpus with tasks
  the machine cannot complete, dragging round 0 toward the near-zero kill
  condition for reasons unrelated to the idea. **State the deviation openly in
  the writeup.**

## Known weaknesses in the corpus, recorded up front

From the 2026-08-12 headroom profiling, so they are not discovered as surprises:

- **Domain diversity is thin.** The brief records this as *"three of five (#2,
  #3, #5)"* — written while the speedup set still had five problems; problem #6
  was confirmed later in the same session, so it is three of six as the corpus
  now stands. Either way they are the same vault/embedding family: distinct
  inefficiencies, so not redundant, but the benchmark is narrow. *(The brief's
  wording is quoted rather than quietly updated — a recorded profiling finding is
  a measurement, and silently editing one is how a record stops being one.)*
- **One speedup problem is a bug-fix, not an optimisation.** "Find the missing
  export" is plausibly a different capability from "vectorise this loop", and its
  ~8.5-minute baseline makes iteration expensive.
- **Roughly 3 problems per type in an 11-problem corpus is thin per-family
  signal.** Accepted cost of wanting a mix rather than one narrow family.
- **Several verifier gates have traps that will silently break the benchmark if
  copied naïvely** — a correct fix that changes an RNG-dependent answer, a
  non-bit-exact training loop needing ~1e-3 tolerance, score drift ~1e-7 after
  vectorising, a "same pass/fail set" gate that the correct fix legitimately
  breaks, and padding changes that shift embedding vectors so equality must be
  cosine > 0.9999. All are enumerated in the brief; read that section before
  authoring any verifier.
- **One of those traps is an undecided policy question, not a detail**: whether
  hardcoding constants instead of exporting them counts as a solve. It is a
  reward-hacking decision and it must be settled *before* the corpus is locked,
  because a verifier cannot be amended afterwards.

---

## Where results land, and what the desktop reads

Everything above this section describes loop 1 as it stood before RES-12: the
contracts and the solver computed the four numbers in memory, and nothing
wrote them to a file the desktop could read. **This section is the exception
to the rest of this document's honesty banner — the files and commands named
below are wired and tested, not aspirational.** A round still cannot be
started end-to-end (§ Running a round above still applies), but once one is,
every attempt now streams its own progress to disk as it runs, not only at
the end.

### Directory layout

```
~/research-results/
  .viewer.json                            points the desktop's metrics pane at "progress"
  loop-<slug>/
    trajectory.json                       (already existed)
    round-00/
      metrics.json                        round summary — per-cell, never pooled
      scores.svg                          grouped bar chart, one bar per cell
      attempts/
        <problem-id>.json                 (already existed — the attempt log)
        <problem-id>/
          metrics.jsonl                   one line per solver step
          metrics.json                    attempt summary
          metrics.chain.json              hash-chain sidecar (see Integrity, below)
          progress.svg
          cap.svg
      checkpoints/                        (already existed)
      escalations/                        (already existed)
```

`attempts/<problem-id>.json` (a file, written by `TrajectoryStore`) and
`attempts/<problem-id>/` (a directory, written by this contract) are
different names on disk and coexist without collision.

### Problem ids and score scales are validated where they are declared

Two values in a corpus definition are written straight into this tree, and
both used to fail far from where they were written — one silently, one at the
cost of an attempt already half-run. Both are checked in `Problem.__post_init__` /
`Verifier.__post_init__` (`contracts.py`), so a corpus carrying a bad one
fails **before the round starts**, at zero compute, rather than after a
workspace has been copied and a solver has run.

**`problem.id` is a path component.** It names `attempts/<id>/`,
`attempts/<id>.json` and `checkpoints/<id>.json`. Nesting stays legal —
`cuda/matmul-speedup` is a supported layout — but an id is refused when it
would escape the results root or collide with something the runner mints
itself:

| refused | why |
| --- | --- |
| `..` in any segment; a leading `/` | leaves the results root |
| an empty segment (leading, trailing or doubled `/`); a whitespace-only one | the path layer discards it, so two ids would name one directory |
| NUL or any other control character | cannot survive the chain header, the log fields, or a terminal |
| a `\` or a `:` anywhere | Windows separator / drive-and-UNC prefix; `fsroots.rs` rewrites `\` to `/` when it reports an entry, and macOS's Finder layer still swaps `:` and `/` |
| a `.`-prefixed segment | `fsroots.rs`'s `should_skip` skips dot-prefixed names, so the run would be invisible to the metrics and images panes |
| a final segment matching `prior-<digits>` | that is the rotation namespace: `_viewer_runs` filters those out of `.viewer.json`, and a shorter id one segment above would rotate its own superseded generation on top of it |
| more than 4 `/`-separated segments | `fsroots.rs` walks at most `MAX_DEPTH = 8` below the root, and a noise-floor seed run already sits four levels down |
| longer than 128 characters | `NAME_MAX` (255) per component and `PATH_MAX` (1024) for the whole path; 128 leaves ~800 characters for the root and the filenames beneath |

Each refusal names the consumer that motivates it, because the rules are not
a naming convention — an operator who trips one needs to know which reader
would have swallowed their run.

**`score_scale` names an emitted metrics key.** The runner writes the raw
score under the problem's own scale (`"speedup":1.99` in the sample lines
below), so a scale that cannot be a metrics key is fatal to the attempt at
its first verification. It stays free-form; what is refused is a scale that is empty,
that wears the `diag_` prefix, or that collides with one of the seventeen
keys every line already carries: `step`, `total_steps`, `ts`, `outcome_code`,
`correctness_pass`, `tokens_used`, `tokens_cap`, `steps_cap`,
`consumed_steps`, `wall_clock_s`, `wall_clock_cap_s`, `cap_extensions`,
`step_wall_clock_s`, `verify_wall_clock_s`, `step_tokens`, `made_progress`,
`progress`. Several of those are plausible scale names — `progress` for a
problem graded on fraction-of-target, `tokens_used` for one graded on token
efficiency — which is exactly why the refusal has to be early and loud. That
list lives in `contracts.RESERVED_METRICS_KEYS` and is imported by
`results.py` (which emits the keys), `integrity.py` (which has to tell a core
field from a score series) and the scale check itself: one definition, three
readers.

### The `metrics.jsonl` line contract

One JSON object per line, appended — never rewritten — as the attempt runs.
The desktop's images and metrics panes tail this file by byte offset, so a
line, once written, is final. Two real lines, verbatim:

```json
{"step":1,"total_steps":50,"ts":1755180000,"outcome_code":0,"correctness_pass":0,"tokens_used":18400,"tokens_cap":1500000,"steps_cap":50,"consumed_steps":1,"wall_clock_s":96,"wall_clock_cap_s":10800,"cap_extensions":0,"step_wall_clock_s":88.2,"verify_wall_clock_s":7.8,"step_tokens":18400,"made_progress":1,"progress":0.0,"speedup":1.0,"diag_peak_rss_mb":412.0,"_chain":"0:daba…"}
{"step":7,"total_steps":50,"ts":1755182140,"outcome_code":1,"correctness_pass":1,"tokens_used":203900,"tokens_cap":1500000,"steps_cap":50,"consumed_steps":7,"wall_clock_s":2236,"wall_clock_cap_s":10800,"cap_extensions":0,"step_wall_clock_s":141.0,"verify_wall_clock_s":24.5,"step_tokens":31200,"made_progress":1,"progress":0.99,"speedup":1.99,"diag_peak_rss_mb":389.0,"_chain":"6:18ab…"}
```

`speedup` is the series name in these two lines **because that is this
problem's `score_scale`** — a loss-driven problem's line would carry
`val_loss` instead, and every problem type names its own raw score
differently. `progress` is the one series comparable across all of them; see
below.

**`consumed_steps` is not `step`, and the two are not interchangeable.**
`step` is the solver's step index — the chart's x-axis, and the field every
other quantity on the line is plotted against. `consumed_steps` is cap
accounting: how many steps the attempt's budget has actually been charged
for. On the happy path shown above they coincide (`step=7`,
`consumed_steps=7`), which is exactly what makes conflating them dangerous —
they legitimately diverge. The runner's `_charge_failed_step` calls
`record_consumption(...)`, which bumps `consumed.steps` **without** bumping
`step_index`; that is the whole purpose of `_spend_carried_on_error`, whose
docstring says a proposal call that happened still costs a step even when
parse/apply then failed. On that path a run can legitimately report
`step=0` alongside `consumed_steps=2`. Deriving one from the other — instead
of emitting both — was a real bug: it made the reconciliation verifier flag
perfectly honest runs as tampered.

`consumed_steps` earns its place on the chart independently, too: a run
where `consumed_steps` climbs while `step` stays flat is burning budget
without making progress, a signal that was previously invisible and is now
its own series.

**Reserved fields.** `step`, `total_steps`, and `ts` are reserved — the
desktop's chart series (`webui/src/desktop/panes/metrics.ts`,
`EXCLUDED_SERIES_KEYS`) always excludes them, because it uses them as the
chart's x-axis and metadata, not as data. A problem-supplied metric that
reused one of those three names would be silently swallowed by the pane, so
the writer refuses it loudly instead: any attempt to put a reserved name, or a
name already used by a core field, into the scored `metrics` payload raises
`ContractViolationError` and stops the attempt. **A field the desktop cannot
chart is a field that does not exist**, and it is better to find that out at
the first emitted line than to discover a silently-missing series after the
run finished.

The same logic explains why `outcome` is not a string on this line. The pane
also drops any non-numeric field, and a string outcome would satisfy "the
attempt's state is recorded" on paper while being invisible on the chart. The
line instead carries `outcome_code`, an integer:

| `outcome_code` | Meaning | Covers `AttemptState` |
|---|---|---|
| `0` | `RUNNING` | `PENDING`, `RUNNING`, `VERIFYING` |
| `1` | `SOLVED` | `PASSED` |
| `2` | `FAILED_WITHIN_CAP` | `FAILED_WITHIN_CAP` |
| `3` | `ESCALATED` | `ESCALATED` |
| `4` | `ABANDONED` | `ABANDONED` |
| `5` | `PAUSED` | `PAUSED` |

`PAUSED` gets its own code rather than folding into `RUNNING` because it is
the state an attempt enters when a subscription window closes, and — per
§ Interruptions above — resumability is load-bearing in this program. An
operator scanning the chart needs to see "working" and "waiting to be
resumed" as visibly different things, not the same flat line.

The four fields `step_wall_clock_s`, `verify_wall_clock_s`, `step_tokens`, and
`made_progress` exist to diagnose *why* the score line looks the way it does,
on the same chart: a run burning tokens with `made_progress` at `0`, or a
`verify_wall_clock_s` that dwarfs `step_wall_clock_s`, is visible without
opening a log file. `cap_extensions` rides on every line because a cap can
grow mid-attempt on an operator's `extend_cap` decision (§ Escalations
above) — a run that reached its target after three extensions is not the
same result as one that reached it inside the original budget, and this field
is what makes the two visually distinguishable on the chart rather than
silently identical.

### The `progress` field

`progress` is a 0 → 1 reading of how far an attempt has come from its own
untouched starting point toward the problem's own passing bar, regardless of
what the underlying metric is called or which direction it moves in. It is
the field `.viewer.json` names as the primary series, specifically so that a
speedup problem, a Kaggle leaderboard problem, and a loss-driven problem are
all readable on one axis without averaging their raw scores together — which
this program never does (§ The four numbers, above).

- The **baseline** is the first score observed in the attempt — the first
  verification run against the untouched workspace, which *is* the
  unmodified starting point. There is no baseline field anywhere in the
  contracts; one was deliberately not added.
- `progress` is present once a target (`PassCriterion.min_score`) exists for
  the problem and a score has been observed against it.
- `progress` is **absent** — not zero, not null-but-charted, simply not a key
  on the line — when the problem has no target at all.
- `progress` is also **absent when the target is at or below the baseline**,
  because the bar was already met before any work happened. Reporting `1.0`
  in that case would draw a solved-looking curve for an attempt that did
  nothing, which is the same fake-curve failure `metrics.py` refuses
  elsewhere in this codebase. This is a refusal, not a bug: it happens
  exactly **once per attempt**, logged at `WARNING` as
  `research.results.degenerate_target`, and every following step goes back
  to a line with no `progress` key rather than a `WARNING` per step.

### The `diag_` namespace

An agent may write its own intermediate numbers — validation-split scores,
memory usage, anything it wants to watch — to `.turing/metrics.json` inside
its own workspace. Every key from that file lands on the next emitted line
prefixed `diag_`. This is the brief's *"the agent invents; the operator holds
the ruler"* rule expressed as a data format rather than a promise: a
`diag_`-prefixed key structurally cannot be mistaken for the score the run is
graded on, because the prefix is applied by the writer, not chosen by the
agent.

The sidecar reader is deliberately permissive, on purpose and asymmetrically
so — see "Integrity, and its limits" below for why the scored `metrics`
payload is strict where this is lenient:

- The file is optional. Missing, unreadable, not valid JSON, valid JSON that
  isn't an object — every failure mode is treated the same way, and the
  attempt is never affected by a broken notebook. A missing file is logged at
  `DEBUG`, because it's the common case and a `WARNING` on every step would
  drown the log.
- Non-numeric, boolean, and non-finite values are dropped silently.
- Capped at **50 keys**, taken in sorted order for determinism. An agent
  writing thousands of series would otherwise make every line enormous and
  the pane unusable; going over the cap is logged once per attempt at
  `WARNING` (`research.results.diagnostics_truncated`), not once per step.
- A file over **256 KiB** is rejected without being parsed at all, logged
  once at `WARNING` (`research.results.diagnostics_too_large`). The agent
  writes this file unattended, so an unbounded read is a denial-of-service an
  operator could otherwise hand to their own overnight loop.

### `.viewer.json`

Written once per round at the results root (`~/research-results/.viewer.json`,
i.e. the parent of every `loop-<slug>/` directory), as:

```json
{"series": "progress", "runs": ["loop-<slug>/round-00/attempts/<problem-id>", "..."], "titles": {"progress": "progress toward target (0 = baseline, 1 = target)"}}
```

`series` names which field the metrics pane treats as primary; it defaults to
`progress` for the reason given above. `runs` are POSIX-style paths relative
to the results root, re-derived from disk on every round rather than kept in
memory — the runner globs `round-*/attempts/**/metrics.jsonl` under the whole
`loop-<slug>/` directory, so the list spans **every round written so far**,
not only the one that just finished. Deriving it from memory instead was a
real bug: the round runner's own attempt outcomes only ever hold the current
round's problems, so a list built from them — even merged into whatever
`.viewer.json` already held — lost every earlier round the moment the process
restarted between rounds and that in-memory state was gone. Re-deriving from
the filesystem on every write costs nothing a restart doesn't already pay.

The walk is depth-independent, because `problem.id` is a path component an
operator may reasonably namespace — `"cuda/matmul-speedup"` produces
`attempts/cuda/matmul-speedup/metrics.jsonl`, two segments below `attempts/`,
and the list includes it at whatever depth it lands. Nesting is supported, not
merely tolerated; what *is* refused, at problem-definition time, is the
narrower set of ids that would escape the results root or collide with a
directory the runner mints itself (see "Problem ids and score scales are
validated where they are declared", below). A rotated
`prior-N/` directory (see Integrity, below) is excluded by name rather than
by depth, since depth no longer tells a live chain from a superseded one once
nested ids are in the mix. Reproduced for real — two rounds, one nested id,
one re-drive that rotates the first round's `cuda/matmul` attempt aside:

```json
{
  "series": "progress",
  "runs": [
    "loop-test-loop/round-00/attempts/cuda/matmul",
    "loop-test-loop/round-00/attempts/s1",
    "loop-test-loop/round-01/attempts/s2"
  ],
  "titles": {"progress": "progress toward target (0 = baseline, 1 = target)"}
}
```

Both `round-00` and `round-01` are listed together, `cuda/matmul` is listed at
its full nested path, and the `prior-1/` directory the re-drive left behind
under `round-00/attempts/cuda/matmul/` is not in the list at all. To watch a
different series by hand — the raw `speedup` or `val_loss` for one problem,
say, instead of the cross-problem `progress` — edit `series` in this file
directly; there is no CLI for it. The desktop re-reads it on its normal poll,
no restart required.

### Integrity, and its limits

Every line also carries `_chain`, a `"<seq>:<sha256 digest>"` string, and a
sidecar file `metrics.chain.json` sits beside `metrics.jsonl` recording the
run's header, its seed hash, and the current chain head. Together they let
`verify_metrics_chain` detect a mutated value, a deleted line, a reordering,
or a fabricated line spliced into the log, and name the exact line index that
broke.

The header is bound into that same chain rather than sitting next to it as
free-floating metadata: `verify_metrics_chain` re-derives `seed_hash(header)`
from the header as read off disk and compares it against the sidecar's
recorded `seed`, instead of trusting that recorded value as the chain head.
**A header edited in place — any of `attempt_id`, `problem_id`, `round_id`,
`seed`, `score_scale`, or `started_at_ms`, with `metrics.jsonl` left
byte-for-byte untouched — is therefore its own FAIL cause**, distinct from
the line-scoped ones above: `reason="metrics.chain.json 'seed' does not
match the recorded header (header altered)"`, with `first_bad_index=None`
because the break is not scoped to any one line — it is the run's identity,
not one of its recorded steps, that no longer matches. A companion check,
`reconcile_summary`, re-derives `metrics.json` from the raw log and
separately catches an honest log with a doctored summary written over it. It
also compares the summary's four identity fields — `attempt_id`,
`problem_id`, `round_id`, `seed` — against the chain header sitting in the
same directory, so a `metrics.json` claiming a different attempt, round or
seed than the log beside it is a named mismatch rather than a clean pass. The
two checks still catch different relabellings and neither subsumes the other:
the header re-hash above catches a header edited to name a different run,
while this catches a *summary* written over a log that was never that run's.
Everything else `reconcile_summary` compares is re-derived by the same reader
the writer used, so for a summary written from that reader — which is exactly
what the contained-crash close-out below writes — agreement is definitional at
write time and those fields can only ever catch a later edit; the identity
fields are the only ones that can catch a dishonest write. `score_scale` and
`started_at_ms` are deliberately excluded: the first is a property of the
problem's verifier rather than of this attempt's identity (and already
load-bearing elsewhere in that module), the second never reaches the summary.
A field absent from either document is skipped rather than flagged — a
missing sidecar is `verify_metrics_chain`'s finding to report, and a summary
that makes no claim cannot contradict one.

**A FAIL has one more cause that is not tampering: a re-run.** Re-driving an
attempt reuses the same `attempts/<problem-id>/` directory — a closed
subscription window, a killed process, or an operator re-driving a round are
all reachable, ordinary reasons this happens, and nothing in `run_attempt`,
`run_round`, or the noise-floor runner checks "is this already done" first.
An honest re-run that simply appended onto whatever `metrics.jsonl` was
already sitting there would splice two attempts' hash chains into one file —
two headers, two `_chain` sequences each restarting at `0`, in the same log.
That is byte-for-byte the shape `verify_metrics_chain` reports for real
tampering, so an operator reading a bare FAIL would have no way to tell a
re-drive from an alteration. `MetricsWriter` closes this at the source rather
than leaving it for the reader to puzzle out: its constructor refuses
outright — raising `ContractViolationError` — if `metrics.jsonl` already
exists and is non-empty, before a single line of the new attempt is written.
The call site rotates the **whole trio** — `metrics.jsonl`,
`metrics.chain.json`, and `metrics.json` — aside together, into a
`prior-<N>/` subdirectory of `attempts/<problem-id>/`, before constructing
the new writer. `N` starts at `1` and increments by finding the first integer
not already in use: a second attempt into the same directory produces
`prior-1/`, a third produces `prior-2/`, and so on — nothing is ever
overwritten or deleted, so a directory that has been re-driven three times
holds three generations on disk at once. Driven for real (fake solver, three
attempts of the same problem into one directory):

```
attempts/s1/
  metrics.jsonl          ← current (3rd) attempt
  metrics.chain.json
  metrics.json
  progress.svg
  cap.svg
  prior-1/                ← 1st attempt, rotated aside before the 2nd started
    metrics.jsonl
    metrics.chain.json
    metrics.json
  prior-2/                ← 2nd attempt, rotated aside before the 3rd started
    metrics.jsonl
    metrics.chain.json
    metrics.json
```

Each `prior-N/` directory is a complete, independently verifiable trio in its
own right — its header carries the superseded attempt's own `attempt_id`, not
the current one's — because the whole trio rotates together rather than just
the JSONL. A rotation that preserved an orphaned chain sidecar with no
summary next to it would only be half a fix.

**The move into `prior-N/` is atomic as a set, not just file by file.**
Moving each of the trio and its plots with its own `rename` call is not
enough on its own: each individual move is atomic, but a process killed
*between* two of them used to leave `prior-N/` holding, say, a log with no
chain sidecar — which fails verification on its own — while the *live*
`attempts/<problem-id>/` directory kept the stale sidecar with no log, a
half-rotated shape `MetricsWriter`'s refusal (checking only `metrics.jsonl`)
did not catch. The next drive then wrote a fresh chain beside that orphaned
sidecar, and **both** generations failed verification on completely honest
data — the exact "verifier cries wolf" failure this document's INCOMPLETE
work went four rounds to eliminate, reopened at a different seam.
`_rotate_stale_metrics` now stages the whole set into a hidden directory
inside `attempts/<problem-id>/` (same filesystem, so every individual move is
still a cheap rename) and commits it with **one** final `rename` onto the
numbered `prior-N/` name — so an observer can only ever see "before" or
"after" rotation, never a `prior-N/` holding some but not all of the files it
moved. A crash can still land inside that staging window, so every call
site — which is to say the start of every attempt — checks for a leftover
staging directory first and finishes committing it before deciding whether a
*new* rotation is needed, closing the window on the next entry rather than
leaving it permanent. `MetricsWriter`'s refusal was widened to match:
constructing a writer now also refuses over a non-empty `metrics.chain.json`
with no `metrics.jsonl` beside it, since that shape is exactly what the
residual staging window can (briefly) leave behind.

**`.rotating/` is a benign staging artifact, not a run.** The hidden
directory the paragraph above stages a rotation's destination into is named
`attempts/<problem-id>/.rotating/`. Between the moment it is created and the
moment it is committed onto its numbered `prior-N/` name, it can hold a real,
honest `metrics.jsonl` — moved there mid-rotation — with no `metrics.json`
summary beside it, because the commit that would give it one has not landed
yet. That is expected, not damage: it is drained by the very next attempt
into the same `attempts/<problem-id>/` directory, which finishes committing
any leftover `.rotating/` before it does anything else (see above), and it is
never left behind by a healthy process — only by one that was killed inside
the staging window. Both tools that discover runs by walking the results
tree know to skip it: `verify`'s `find_runs` and the desktop's
`.viewer.json`-writing `_viewer_runs` both exclude any path with a
`.rotating` segment, the same way they already exclude `prior-N/` (by name,
not by depth). An operator who finds a `.rotating/` directory sitting next to
a live attempt does not need to clean it up — the next re-drive does that on
its own — and should worry only if it is present as anything *other* than a
directory, which `_rotate_stale_metrics` refuses outright, by name, as a
`ContractViolationError` rather than a bare `FileExistsError`.

**Telling a re-drive apart from an alteration.** `verify`'s directory walk
finds *every* directory anywhere under `<path>` containing a file literally
named `metrics.jsonl`, at any depth — which means it walks into `prior-N/`
subdirectories too and prints a separate `OK`/`FAIL` line for each one. **A
rotated-aside file is not invisible to `verify` and can itself report a
FAIL** — the discriminator an operator actually has is *which path* the FAIL
line names, not whether one exists at all. Run for real against a directory
holding a current attempt plus two rotated-aside ones:

```
$ .venv/bin/python -m turing.research.loop.verify <results-root>
.../attempts/s1: OK (4 line(s) checked)
.../attempts/s1/prior-1: OK (4 line(s) checked)
.../attempts/s1/prior-2: OK (4 line(s) checked)
detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11
```

The presence of one or more `prior-N/` siblings next to the current
`metrics.jsonl` is the on-disk signature of a re-drive; every honest
generation there, current or rotated, verifies clean independently. A FAIL
against the **current** attempt's own `metrics.jsonl` — the path `verify`
prints with no `prior-N` component — means the record you are relying on
*right now* is corrupt or altered, exactly as the rest of this section says.
A FAIL against a `prior-N/` path means that one **superseded** generation is
corrupt; it does not implicate the current attempt, but it is also not
automatically explained by "it's just a re-drive" — read on for the one
reachable, genuinely benign way that happens, and note that this module
cannot tell it apart from tampering on its own.

**Verifying an attempt that is being written right now is `INCOMPLETE`, not
`FAIL` — this used to be a false alarm and is now a state.** `MetricsWriter`
writes the JSONL line first and rewrites `metrics.chain.json` second —
deliberately, not incidentally: if the sidecar write failed after an honest
line landed, the *next* verification must see a line-count mismatch and say
so, rather than a writer whose counters silently drifted from what is on disk
until someone eventually ran `verify` and got a confusing answer far removed
from the actual failure. That ordering is not negotiable and has not changed;
neither has the fact that it leaves a real window in which `metrics.jsonl` has
been extended but the sidecar has not yet caught up. Two more windows exist
beside it: the sidecar rewrite is a truncate-then-write, so a reader can catch
it *empty*, and `Path.read_text` reads a long log in chunks, so a reader can
catch the writer's own append part-way and see a truncated **final** line.

Since this document positively invites pointing `verify` at a round *while it
runs*, all three were reachable in ordinary use, and all three used to report
`FAIL` — exit `1`, the code that means "a real failure, act on it" — on
completely honest, in-flight data. Measured against a real writer and a real
verifier, the version of this section you may remember was understating it:
1633 of 1640 checks failed, 1284 of those on the truncated sidecar rather than
on the line-count mismatch that was originally reported. On a writer appending
at a more attempt-like pace (one line every 20 ms), 504 of 6527 checks — 7.7%
— failed. This is the same "a verifier that cries wolf teaches its operator to
ignore it" failure the rest of this layer exists to eliminate, so it is fixed,
in the verifier, without touching the write ordering:

**`verify_metrics_chain` re-reads before it believes a failure a writer could
have caused.** A failure pinned to an *interior* line is never re-read at all —
the log is strictly append-only and no append rewrites a byte before the end,
so that is exactly where a real tamper shows up and it fails immediately, with
the same reason and the same `first_bad_index` as always. Everything else —
any failure about the pair as a whole, or one pinned to the last line on disk —
gets one more look 50 ms later. Three outcomes, all of them honest:

* the writer finished in the meantime → `OK`;
* the two files are byte-identical to the previous read and still do not
  verify → nothing is writing them, the damage is real → `FAIL`, exit `1`,
  unchanged. This is what a crash between the two writes leaves, and it is
  separable from the race by *time* alone;
* the files keep changing under the verifier → `INCOMPLETE`, exit `2`.

That last state is what a round under continuous append reports, and it is
reusing the existing exit contract rather than adding to it: a live attempt has
no `metrics.json` either, so `2` — "nothing failed, but these numbers are not
final" — was already the right answer for it. Real output, against an attempt
being appended to as fast as the writer can go:

```
$ .venv/bin/python -m turing.research.loop.verify <results-root>
<results-root>/attempts/race: INCOMPLETE (13124 line(s) read, chain still being written) metrics.jsonl has 13124 line(s), metrics.chain.json reports 13102; the pair changed under all 3 read(s), so a writer is appending and no consistent snapshot was available (not a finding about the data)
detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11
$ echo $?
2
```

Note what that line does *not* say: not `FAIL`, and not "chain intact" either —
the chain was never read consistently, and claiming otherwise would be a
different kind of dishonesty. Stop the writer and re-run against the very same
directory and it settles into the ordinary unfinished-attempt shape, still
exit `2`:

```
<results-root>/attempts/race: INCOMPLETE (43540 line(s) checked, chain intact) metrics.json is missing (the attempt never reached its summary write)
```

Across the two writer profiles measured above, the fixed verifier reports 0
failures out of 1202 checks and 0 out of 22 — the attempt-paced one a clean
`OK` every time, the hammered one `INCOMPLETE` every time.

**Nothing was loosened to get there**, and the re-read opens no new evasion.
The only new verdict reachable is `INCOMPLETE`, and reaching it requires the
files to *change* between reads — i.e. a process writing them during
verification. Such a process cannot produce `OK` by changing bytes, because
`OK` is decided by recomputing every digest from the header; an attacker who
can write a self-consistent chain does not need this path at all (that hole is
conceded above, and it lands on the *first* read). The one move a live writer
buys is holding the verdict at `INCOMPLETE` for as long as the process runs —
trading a permanent quiet green for a loud non-zero that names the directory
and says a writer is active. That is not a gain.

**The residual, stated plainly:** a writer descheduled for longer than the
50 ms pause *between* its two writes still produces a `FAIL`. That window is a
couple of syscalls wide against a pause three to four orders of magnitude
larger, so it is rare rather than closed, and the honest fix for it is an
atomic sidecar replace at the writer — not more waiting in the verifier. An
operator who does see a `FAIL` naming a **currently-open** attempt (no
`prior-N` in the path, `INCOMPLETE` summary alongside) can still re-check
before escalating; that is now the exception rather than the routine outcome.
A `FAIL` against a `prior-N/` path, or one paired with a **present,
disagreeing** summary, is not this shape and should not be waited out.

**A killed process can produce a rotated-aside FAIL that is not tampering,
and `verify` cannot tell the difference.** `MetricsWriter.append` is not
atomic across a process kill: if the process dies mid-write, `metrics.jsonl`
is left with a truncated, malformed final line. Rotation does not validate
what it moves — `_rotate_stale_metrics` checks only that the file exists and
is non-empty, never that it parses — so that malformed line survives the move
into `prior-N/` unchanged, and the *next* attempt (which never touches the
malformed file) is unaffected and verifies clean on its own. Reproduced for
real: after truncating a completed attempt's `metrics.jsonl` mid-line to
simulate a kill, then driving a second attempt into the same directory:

```
$ .venv/bin/python -m turing.research.loop.verify <results-root>
.../attempts/s1: OK (4 line(s) checked)
.../attempts/s1/prior-1: FAIL chain: line 2 is not valid JSON (first_bad_index=2); summary: 5 field(s) disagree with the raw log (mismatches=['steps_recorded', 'consumed_steps', 'consumed_tokens', 'consumed_wall_clock_seconds', 'outcome'])
detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11
```

Nothing in this mechanism distinguishes that shape from an actual tamper of
the rotated file — both look identical on disk and to `verify_metrics_chain`.
An operator seeing a FAIL on a `prior-N/` path has to reach for context this
module does not have (was that attempt running when a subscription window
closed or a process was killed?) rather than reading the FAIL alone as proof
either way. This is a real limitation, not a reassurance: **a FAIL on a
rotated-aside generation is not automatically benign just because it is
rotated aside.**

**A process killed *between* writes produces a different shape, and that one
`verify` does name: `INCOMPLETE`.** `metrics.json` is written once, when an
attempt terminates; `metrics.jsonl` and its sidecar are extended after every
step. Kill an attempt in the gap between two appends — a closed subscription
window, this program's own stated normal — and what is left is an intact,
fully verifiable chain with no summary beside it. Rotation moves only the
files that exist, so the operator's honest re-drive leaves a `prior-N/`
holding a good chain and no `metrics.json` **for as long as the results root
survives**. That is not a failure and never becomes one, so reporting it as
`FAIL` made the whole root exit `1` forever on untouched data. It is its own
state, with its own exit code (`2`). Reproduced for real, after cancelling an
attempt mid-flight and then re-driving it to completion:

```
$ .venv/bin/python -m turing.research.loop.verify <results-root>
.../attempts/s1: OK (4 line(s) checked)
.../attempts/s1/prior-1: INCOMPLETE (1 line(s) checked, chain intact) metrics.json is missing (the attempt never reached its summary write)
detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11
$ echo $?
2
```

The two killed-process shapes are therefore reported differently and must be
read differently: a truncated chain is damage `verify` cannot tell from a
tamper (above), while a missing summary over an intact chain is simply a run
that did not finish. **Only the summary's absence is excused this way.** A
`prior-N/` whose chain does not recompute is `FAIL` whether or not a summary
sits beside it — otherwise deleting one file would soften a tamper's exit code
from `1` to `2`.

**`INCOMPLETE` used to over-fire, on a shape that is not a killed process at
all: a *contained* attempt crash.** Round containment (see "One attempt's
failure is contained to that attempt", above) catches an attempt's exception
inside `run_attempts` and lets the round finish — but the crashed attempt's
own `metrics.json` was, until this fix, never written, because that file has
always been written by `run_attempt`'s own normal completion, and containment
means `run_attempt` never reaches its end. A round that finished, having lost
one problem to a contained crash, left that problem's directory in exactly
the "intact chain, no summary" shape `INCOMPLETE` exists to name — permanently,
since nothing was ever going to re-drive a directory the round considers
closed — so a pre-writeup gate demanding exit `0` could never pass on a tree
where nothing was wrong that the round's own verdict had not already
disclosed. Nothing here was loosened to fix that: `RoundRunner` now writes a
terminal, log-derived `metrics.json` for a contained crash
(`runner._close_out_crashed_attempt`) — `best_score: null`, since the attempt
produced no grade and a floor value would be the same fabricated-regression
bug the round's cell computation already refuses one level up, and
`final_state: "crashed_in_harness:<ExcType>"`, deliberately not a value of
`AttemptState`, so it can never be misread as an outcome the experiment
measured. Reproduced for real, a backend reporting a non-finite token count
mid-attempt (after one line was already on disk — the attempt checkpoint's
`allow_nan=False` writer refuses it) inside an otherwise-normal round:

```
$ .venv/bin/python -m turing.research.loop.verify <results-root>
.../attempts/bad: OK (1 line(s) checked)
.../attempts/good: OK (4 line(s) checked)
detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11
$ echo $?
0
```

`bad`'s own `metrics.json` there reads `"best_score": null,
"final_state": "crashed_in_harness:ValueError"` — a finding, not
a pass, but a *finished* one, and `verify` now agrees. The round record still
says exactly what happened (`all_attempts_completed: false`, the verdict
prefix, no delta for the affected cell — see "A round record / trajectory"
below); only the exit code stopped conflating "this round disclosed a loss"
with "this attempt has not finished yet."

**The close-out refuses to write when the chain in that directory is not this
attempt's** — a guard its docstring promised from the start and that round 3
found unimplemented. `_read_crashed_attempt_record` compares the chain
header's `attempt_id`, `problem_id`, `round_id` and `seed` against the attempt
that actually crashed, and refuses (logging
`research.results.crashed_attempt_summary_refused` at `ERROR`, writing
nothing) when any of them disagrees. This is reachable rather than defensive:
`run_attempt` materialises the workspace — real I/O against a template that
can be pruned, evicted, or hit a full disk — **before** it calls
`_rotate_stale_metrics`, so a crash in that window finds the *previous*
generation's chain still sitting in place, intact and summary-less. Driven,
the unguarded close-out adopted it: generation 1 (`run_id=gen1, seed=7`) was
killed before its summary, generation 2 (`run_id=gen2, seed=99`) crashed in
`materialise`, and the summary written over generation 1's log read
`{"attempt_id": "gen1-beta-…", "round_id": "gen2", "seed": 99, "final_state":
"crashed_in_harness:OSError"}` — asserting that generation 1 died of an
exception that never touched its log — while `verify` reported `OK` with zero
mismatches, because every field it reconciled had just been re-derived from
that same log. It also destroyed the honest `INCOMPLETE` state that said
generation 1 was killed unfinished, which is the one thing rotation promises a
superseded generation keeps. With the guard, that directory stays untouched
and keeps reporting `2` until someone looks at it. `reconcile_summary`'s
identity check (see "Integrity, and its limits") is the independent second
guard on the same shape, from the reader's side.

**`attempt_id` is the field that decides it, and round 3's version left it
out.** Round 3 pinned the comparison on `problem_id`, `round_id` and `seed`
alone, on the grounds that `attempt_id` was minted inside `run_attempt` and
died with the exception, and conceded a re-drive repeating `run_id` *and*
seed as the guard's documented limit — framed as exotic. It was the shipped
recovery workflow. `NoiseFloorConfig.round_config_for` derives each seed's
`run_id` deterministically as `<run_id>-seed-<n>`, and the refusal an
incomplete seed raises tells the operator, in those words, to *"fix the cause
and re-run the noise floor from seed 1"* — so the sanctioned recovery reuses
the identical `run_id`, the identical seed and the identical
`noise_floor_seed_dir`, every time. Driven end to end: seed 1's `speed-1`
attempt is killed in the gap between its last append and its summary write
(`INCOMPLETE`, exit `2`); the operator re-runs the floor as instructed; that
seed's `speed-1` crashes in `materialise`, before rotation; all three of round
3's fields match, the guard passes, and the close-out writes
`crashed_in_harness:OSError` over generation 1's log — a cause of death
generation 1 never experienced — flipping the directory to `OK` and the whole
results root to exit `0`. The honest "killed, unfinished" record is destroyed
and `verify`, the pre-writeup gate, blesses the result.

The fix is to stop the id dying: `run_attempts` mints it (`_mint_attempt_id`)
*before* calling `run_attempt` and passes it down, so it survives the
exception in the caller's frame and reaches the close-out. Its `uuid4` suffix
differs between two generations of the same attempt even when every other
identity field repeats, so the comparison is now exact rather than a
best-effort trio; `run_id` determinism is untouched, and nothing that depends
on a floor being re-runnable under the same ids changed. The other three
fields stay in the comparison because they are what makes the refusal
*readable* — `round_id='gen1' (expected 'gen2')` tells an operator which
generation is sitting in the directory, where a bare id mismatch would not.

`started_at_ms` — also in the header, and different for every generation —
cannot serve this purpose, which is why the id is minted upstream instead.
`run_attempt` awaits `materialise` *before* it reads the clock and builds the
`Attempt`, so at the canonical crash point no start time has been taken at
all; and the nearest substitute, a watermark stamped by the caller and
compared with `>=`, would rest on wall-clock monotonicity *across processes*,
which `SystemClock.now_ms` (`time.time()`) does not provide. An NTP step, a
container clock reset, or a re-run landing in the same millisecond would make
a previous generation's header look current and the guard would adopt exactly
the chain it exists to refuse. A `uuid4` needs no clock and no ordering
assumption. Note that this is a *different* comparison from
`reconcile_summary`'s identity check, which excludes `started_at_ms` for an
unrelated reason — that one compares two **documents** in the same directory
and the field never reaches a summary, while this one compares the header
against the **live attempt** that just crashed. Different sides, different
evidence; neither field list is the other's.

What remains indistinguishable is only what no evidence can separate: a
`uuid4` collision in the 32-bit suffix (and that fails *closed* — a matching
id on a foreign chain is the one direction that adopts, at roughly one in four
billion), and a re-drive against a results tree an attacker can write to,
which is the R2/Q11 limit the honesty statement at the end of this section
already states for every check in this module.

**`materialise` was left where it is, before rotation.** Reordering would
close this window too, but it costs more than it buys: rotation would then
fire for an attempt that never starts, minting a fresh `prior-N/` on every
failed re-drive and moving the live generation's chain, summary and plots out
of the directory the desktop reads in favour of a generation that produced
nothing. The identity guard closes the defect without inventing empty
generations.

**Exit `2` still means what it always meant: a killed process, not a
contained crash.** Nothing above shrinks `INCOMPLETE`'s trigger — an attempt
that never reaches *either* its normal completion *or* a caught exception in
`run_attempts` (the process itself died, taking the whole call stack with it)
still leaves an intact chain with no summary, and still reports `2`.
Reproduced for real, deleting a completed attempt's own `metrics.json` to
stand in for a process killed in the gap `run_attempt`'s own docstring names —
after the last append, before the summary write:

```
$ .venv/bin/python -m turing.research.loop.verify <results-root>
.../attempts/killed: INCOMPLETE (4 line(s) checked, chain intact) metrics.json is missing (the attempt never reached its summary write)
detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11
$ echo $?
2
```

The dividing line is not "did something go wrong" — it is "did the round (or
the noise floor, or the CLI operator watching it) ever get a chance to say
so." A contained crash always does, through the channels named above; a
killed process, by definition, cannot.

**A read-only `attempts/<problem-id>/` directory makes the re-drive fail
loudly, not silently — but "fail" now means two different things for a round
and for a noise floor, and the two must not be conflated.** `_rotate_stale_metrics`
is called at the very start of `run_attempt`, before a single line of the new
attempt is written, and it is **not** wrapped in the try/except that guards
the summary and plot writers later in that function — so the `OSError` it
raises (`prior_dir.mkdir(...)` denied) still propagates straight out of
`run_attempt` itself, uncaught, exactly as before. What changed is what is
sitting one level up. `run_attempts` now wraps *every* call to `run_attempt`
in the same per-attempt guard that contains every other attempt-local crash
(see "One attempt's failure is contained to that attempt", above), and a
rotation `OSError` is an ordinary `Exception` to that guard — it does not know
or care which line of `run_attempt` raised it.

- **A round completes anyway, minus the one problem.** Reproduced for real,
  re-driving a directory whose rotation raises `PermissionError`: `run_round`
  logs `research.attempt.crashed` and `research.round.attempts_lost` at
  `ERROR`, sets the `all_attempts_completed` gate to `False`, and prefixes the
  verdict —
  ```
  1 of 2 attempt(s) failed and are absent from every cell (bad); speedup/practice: no parent round; a baseline has no marginal gain
  ```
  — with `bad`'s *own* `attempts/bad/metrics.json` left exactly as the
  re-drive found it: rotation raises before touching a single file, so the
  previous, complete summary is simply never disturbed, not synthesised.
  `python -m turing.research.loop.verify` on the finished results root exits
  `0` — `bad`'s untouched old chain and its untouched old summary still
  reconcile with each other, same as they did before anyone tried to re-drive
  it. The round is honest about the loss (the gate, the verdict prefix, the
  refused deltas — see "A round record / trajectory" below), but it is not
  unfinished, and `verify` must not say it is.
- **A noise-floor seed refuses instead, on purpose.** The same containment
  happens inside `run_attempts` — the `PermissionError` does not propagate out
  of it there either — but `NoiseFloorRunner.run` checks `attempt_failures`
  immediately afterward and raises rather than accept a partial seed:
  ```
  noise-floor seed 1 (run 'noise-floor-seed-1') lost 1 of 2 attempt(s): bad
  (PermissionError: [Errno 13] Permission denied: '.../attempts/bad/prior-1').
  The floor is refused rather than reduced from what survived: ...
  ```
  `noise-floor.json` is never written. See "The noise-floor caller has a
  stricter bar than this" in `run_attempts`'s own docstring for why a round
  tolerates exactly the asymmetry a floor cannot: a floor measured over a
  problem set that shifted between seeds measures the set, not the agent, and
  every later round's saturation call depends on it being the same yardstick
  for all of them.

Neither behavior is the pre-containment one — a bare, uncaught `PermissionError`
straight out of `run_round` or `NoiseFloorRunner.run`. A round's job is to
survive one broken problem and disclose the loss everywhere a reader would
look; a noise floor's job is to be the identical measurement across every
seed or to refuse to be a floor at all, and containing the exception at the
same layer a round does would silently hand it a floor computed over fewer
problems than its sibling seeds.

**Read this before trusting a green result from either check, because it is
the honesty statement this whole mechanism is built on and it is not
softened anywhere else in this codebase either:**

> This module makes tampering **detectable**. It does not make it **impossible**,
> and no docstring, log line, or CLI output in this codebase may claim that it
> does. `OPEN-QUESTIONS.md` **R2** records that the `turing` sandbox user is
> provisioned and never used — `research_workspace_root` defaults to the
> *operator's* home and nothing drops privileges — so agent-authored code runs
> as the operator today. `verify_metrics_chain` re-derives the header's hash and
> compares it against the recorded seed, so editing the header alone —
> `attempt_id`, `problem_id`, `round_id`, `seed`, `score_scale`, or
> `started_at_ms`, with every digest and every byte of `metrics.jsonl` left as
> it was — is now a nameable FAIL, not a silent pass. What still verifies clean
> is the *full* recomputation: a process that can write anywhere the operator
> can write can rewrite this entire chain from a forged header down —
> recomputing every digest from it and rewriting the sidecar to match — and
> produce a file that verifies clean. Nothing in this module can tell that
> apart from a genuine run, because the header is bound *into* the chain and
> never anchored to anything outside it, so a self-consistent forgery is
> indistinguishable from a real one by construction. Prevention needs R2
> (privilege separation, results root not
> writable by the agent) and **Q11** (harness physically unreachable). Until
> those land, a green verdict from this module means "not casually altered", not
> "provably genuine".
>
> Second, narrower point that must not be blurred: a valid chain proves a number
> was not changed **after it was recorded**. It says nothing about whether the
> number is **meaningful**. A wrong verifier produces wrong numbers that chain
> perfectly.

A reader who takes a green `verify` result as proof that a number is real has
been misled by this document. It is not proof. It is a tripwire against
casual, accidental, or unsophisticated alteration, running on a machine where
the same account that could alter the record also produced it.

**Running the check:**

```bash
.venv/bin/python -m turing.research.loop.verify <path> [--json]
```

`<path>` can be one attempt directory, one round, one `loop-<slug>/`
directory, or the whole results root — the CLI walks it for every directory
containing a `metrics.jsonl` and checks each one. Human-readable output is one
`OK` / `INCOMPLETE` / `FAIL` line per run, with a reason and the failing line
index (or the mismatch list) on `FAIL`. `--json` prints one JSON object per run
instead, for scripting; each record carries both `ok` (true only for `OK`) and
`state` (`"ok"` / `"incomplete"` / `"failed"`). The nested `chain` object
carries the same pair, where its `state` is `"ok"` / `"in_flight"` /
`"failed"` — `"in_flight"` meaning the chain could not be read consistently
because a writer is appending to it, which is a distinct fact from a chain
that failed to recompute.

**Exit code — three values, not two:**

| code | meaning |
| --- | --- |
| `0` | every run in `<path>` is complete and passes both checks |
| `1` | at least one run **failed**: a broken chain, or a summary that is present and disagrees with its log. Also: no runs found at all |
| `2` | nothing failed, but at least one run is **incomplete**: an intact chain with no `metrics.json` beside it, or a chain a writer is still appending to |

`1` outranks `2` when both are present. An operator wiring this into a
pre-writeup check needs the exit code to mean something on its own, without
reading the text — and it needs three values, because `metrics.json` is
written once when an attempt ends while the chain grows after every step.
Verifying a round *while it runs*, or a root holding an attempt that a closed
subscription window killed, finds intact chains with no summary beside them
through nobody's fault. Worse, that shape is **permanent**: the honest
re-drive rotates the killed generation's trio into `prior-N/` exactly as it
found it — chain, no summary — so a two-value exit code reported the whole
results root as failing forever, on data nobody touched. `2` says "nothing is
wrong here, but something is not finished", which is neither a green light nor
an accusation. It carries one more shape for the same reason: an attempt whose
log is being appended to *as the verifier reads it* cannot be shown a
consistent snapshot at all, and reporting that as `1` was the second false
alarm this tool had to stop emitting — see "Integrity, and its limits" above.
Every invocation, clean or tampered, also
prints a trailing line stating the limitation above in full:
`detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11`. That
line is not decoration; a tool that printed a bare `OK` would be read as a
claim of authenticity it cannot back.

**The verdict badge — the same check, where the numbers are actually read.**
An operator reading a curve in the desktop's metrics pane is not running this
CLI, and the pane cannot run it (this is Python; that is a WebView). So the
loop runs it *itself*: `results.write_attempt_verdict` calls the same
`verify_run` at the end of every attempt, immediately after `metrics.json` is
written, and records the answer beside it in `metrics.verdict.json`:

```json
{
  "schema_version": 1,
  "state": "ok",
  "lines_checked": 41,
  "checked_at_ms": 1786845156000,
  "checked_by": "loop",
  "chain_head": "9f2c…",
  "detail": "…/attempts/s1: OK (41 line(s) checked)",
  "note": "detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11"
}
```

`state` and `detail` come from `verify_run` and `format_run_verdict` — the
same two functions this CLI calls — so the badge and the terminal can never
tell an operator two different stories about one run. The file is a *report
about* the run, never part of it: the chain does not cover it, `find_runs`
does not see it (that walk keys on `metrics.jsonl`), reconciliation does not
read it, and re-running `verify` after it lands returns exactly what it
returned before. A re-drive rotates it into `prior-N/` with the trio, since a
verdict left in place would describe the superseded generation's log. A
directory with no `metrics.jsonl` gets no verdict at all — it is not a run,
and stamping `failed` on it would manufacture a finding.

The pane compares `lines_checked` against the number of lines it parsed
itself, so a log that has grown since the check reads `stale` rather than
green — see `desktop/README.md`'s "The `metrics` pane's verdict badge" for the
five badge states. The green one reads `chain ok`, never "verified", and
carries this qualifier in its tooltip: *chain internally consistent as last
checked by the loop — not proof the numbers are authentic or meaningful; run
`python -m turing.research.loop.verify <dir>` for an independent check*.
Everything this section says about what a green `verify` does and does not
mean applies unchanged to a green badge, which is why the badge says so
itself.

### A round record / trajectory: a lost attempt refuses deltas, not cells

A round that loses an attempt to a contained crash (see "One attempt's
failure is contained to that attempt", above) still reduces every problem
that *did* finish into its cells — the round is not thrown away — but it must
not let that partial measurement pose as a full one to whatever compares it
against its parent. Reproduced for real: round 0 measures `alpha` at `3.0` and
`beta` at `0.5`, a cell mean of `1.75` over `n=2`; round 1 runs the identical
corpus, loses `beta` to a contained crash, and measures `alpha` alone at
`3.0`. The arithmetic that would read as a `+1.25` gain is real — a mean rises
when its weakest member drops out — and nothing about it would be the agent
improving.

**The round-wide `SaturationVerdict.REFUSED_ATTEMPT_LOST`** is what stands
between that arithmetic and the trajectory. `assess_saturation` and
`compute_deltas` both refuse *every* cell in a round with a non-empty
`attempt_failures`, not only the cell the lost problem belonged to —
`cost_per_unit_gain` divides one round-wide wall clock and a token total the
lost attempt's own spend never entered, so every cell's cost number is
contaminated by the loss regardless of which cell it fell in. The refusal
reports no gain at all, not a flagged one: `marginal_gain` stays `None` on
this verdict specifically (unlike the two floor-refusal verdicts, which still
report a real, floor-less gain), because a difference of means taken over a
different problem set on each side is not a measurement, and reporting it
anyway is exactly how a `+1.25` phantom would reach a verdict line. Real
`trajectory.json` row for round 1 above:

```json
"delta": {},
"delta_in_noise_units": {},
"cost_per_point": {},
"saturation": [
  {
    "problem_type": "speedup",
    "split": "practice",
    "verdict": "refused_attempt_lost",
    "reason": "speedup/practice: at least one attempt was lost, so this round measured fewer problems than its parent; a difference of means over a shifting problem set measures the set as much as the agent and no delta is reported",
    "marginal_gain": null,
    "noise_floor": null,
    "gain_in_noise_units": null
  }
],
"verdict": "1 of 2 attempt(s) failed and are absent from every cell (beta); speedup/practice: at least one attempt was lost, so this round measured fewer problems than its parent; a difference of means over a shifting problem set measures the set as much as the agent and no delta is reported"
```

Note what is present alongside that refusal in the same row:
`"constraints": {"all_attempts_completed": false, "eval_set_stable": true, ...}`
and `"trajectory_restart": false`. **The round lost an attempt; it did not
lose its eval set**, and the row says both facts separately rather than
folding a lost-attempt round into the same bucket as an eval-set change. That
distinction is deliberate, not incidental, because `trajectory.py` derives
`trajectory_restart` from `eval_set_stable` alone: an eval-set change genuinely
restarts the curve (nothing before it is comparable to anything after), while
a lost attempt does not — the next round still descends from this one, still
measures the same corpus, and a re-drive of the lost problem alone would make
this round's own gap fillable in place. Collapsing the two would make a
transient harness failure look like the eval set itself had to be redefined,
which is a strictly worse read of what happened.

**Two fields named `comparable_to_parent` answer two different questions, on
purpose, and they legitimately disagree on exactly this round.**
`trajectory.json`'s row reports `"comparable_to_parent": true` — it answers
*"did the parent measure the same eval set?"*, computed from nothing but the
two eval-set hashes, because that field alone is what `trajectory_restart`
must derive from and a lost attempt must not trip it. The round's own
`metrics.json` summary reports `"comparable_to_parent": false` for the same
round — it answers a different, stricter question a reader of *that* file is
actually asking: *"may these numbers be compared with the parent's?"* — for
which a matching eval-set hash is necessary but not sufficient, since this
round's cells were reduced over a strict subset of the problems the parent's
were reduced over. Both files are correct simultaneously; a reader who
expects the same field name to mean the same thing in both has been led
wrong by the coincidence of the name, not by either file. Real output, same
round, both files:

```json
// trajectory.json, round 1's row
"comparable_to_parent": true

// round-01/metrics.json — the round summary
"comparable_to_parent": false
```

The round summary's `cells` array is the machine-readable half of the same
refusal: a clean round's cells each carry `marginal_gain`, `noise_floor`, and
`cost_per_unit_gain`; a cell belonging to a round with a non-empty
`attempt_failures` carries none of the three, so a downstream reader — the
desktop pane, a pre-writeup gate, the self-edit seam deciding whether to keep
going — can tell "this round's delta is not a valid comparison" without
parsing the verdict's English.

**The same refusal reads off the parent, and without that half it was
one-sided.** Nothing consulted the *parent* record's `all_attempts_completed`
gate, so a round that lost an attempt correctly refused its own deltas and
then served as the baseline for the next round's. Driven end-to-end: round 0
loses `alpha` (the *strong* problem) to a contained crash and records a cell
mean of `0.5` over `n=1`, refusing its own reporting exactly as above; round 1
then runs the identical corpus with the identical agent and identical scores,
records `1.75` over `n=2`, and emitted `marginal_gain: +1.25`,
`cost_per_unit_gain: 0.2959`, `SaturationVerdict.IMPROVING`, a verdict line
reading `"gain +1.25 = +12.50x the noise floor"` and
`"comparable_to_parent": true` — **with every gate green**. The agent did not
change at all; the entire delta is *which problems ran in the parent*. Losing
the weak problem in the parent manufactures the mirror-image phantom
regression.

`run_round` now reads the parent record's own `all_attempts_completed` gate
and passes it to `compute_deltas` and `assess_saturation`, which refuse
round-wide on the same argument, and the refusal has its **own** verdict —
`SaturationVerdict.REFUSED_PARENT_ATTEMPT_LOST` (`"refused_parent_attempt_lost"`
in `trajectory.json`) — rather than sharing `refused_attempt_lost`. The two
call for different operator actions: `refused_attempt_lost` says re-drive the
problem *this* round lost, while `refused_parent_attempt_lost` says this round
is fine and the *baseline* is not, so no amount of re-driving this round will
bring the delta back — the parent has to be re-measured, or a later complete
round adopted as the baseline. When both rounds are lossy the round's own loss
is the verdict (its gate and verdict prefix already say so) with the parent's
named in the same reason string, so nobody re-drives this round expecting the
delta to return. `ERROR`-level `research.round.parent_attempts_lost` carries
the same fact to the log, naming the parent's `run_id`.

**A parent record carrying no `all_attempts_completed` gate at all is refused,
not trusted.** A record written before that gate existed is silent about the
one fact the refusal turns on, and the two readings of that silence are not
symmetric: trusting it re-opens exactly the phantom gain above — emitted with
every gate green, which is the property that makes it dangerous — while
refusing it costs a delta that returns the moment the baseline is re-driven
under a runner that writes the gate. Every other unresolved basis in
`metrics.py` (no floor, a degenerate floor, a changed eval set) already
resolves the same way. The refusal reason says which case it is:
`"the parent round records no all_attempts_completed gate, so whether it
measured the full corpus is unknown"`.

The parent's completeness is deliberately **not** added to this round's
`gates` map. Every entry there is a statement about the round it belongs to,
and `webui/src/desktop/panes/flywheel.ts` reads any `false` gate it does not
recognise as *this round failed something* — which would paint a complete,
honest round red for its baseline's fault. The refusal travels in the channel
built for refusals instead (`saturation[].verdict`, the empty `delta` /
`delta_in_noise_units` / `cost_per_point` maps, the verdict line, and
`comparable_to_parent: false` in the round summary), and that pane already
renders any `refused_*` verdict as "refused" with no change needed. The round
summary's `comparable_to_parent` is the one place the two losses meet: it
answers *"may these numbers be compared with the parent's?"*, so it is
`false` when this round lost an attempt, when the parent did, or when the
parent is silent about whether it did.
