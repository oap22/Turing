# Research Journal — Turing

Newest entry at the top, immediately under this header. **Never edit or delete a
past entry** — a wrong entry is corrected by a *new* entry that links back to it,
because the record of having been wrong is part of the record.

On-disk spec: `~/Developer/active/skills/skills/research-loop/conventions.md`.
Loop-specific rules: `driving-functions.md` in the same directory.

**Nothing in this program has been measured yet.** The first entry carrying
numbers will be the noise floor, and per `driving-functions.md` § The Noise Floor
Comes First, it has first claim on the budget — measuring it late makes every
earlier number uninterpretable retroactively.

**Standing rule for every entry from here on: log estimate vs actual**
(wall-clock, subscription consumption, solve rate). The operator has attempted
nothing in this direction before, so every prior in the brief is borrowed from
the literature rather than from experience — estimate-vs-actual from the very
first run is how real priors start accumulating.
(Brief § Priors & Dead Ends.)

---

## 2026-09-04 — First live RSI workstation run: import-speedup, 4 rounds, one self-edit, 4.77× measured

**This is the first entry in this program that carries measured numbers.** It
is a run of the *RSI workstation loop* (`python -m turing.research.rsi`, new in
PR #413), not of the loop-1 corpus runner — so it is not the noise floor the
header above promises, and nothing here is a round-0 solve rate. One problem,
one sandbox, one frozen verifier, one trajectory. **One trajectory is an
anecdote** (ADR 0011, Consequences); read the numbers as a smoke test of the
loop's machinery, not as evidence about self-improvement.

### Hypothesis
A frozen-verifier loop can drive an agent to a real, measured speedup on a
problem sourced from this repo (ADR 0011 §11: speedup problems come from the
operator's own code), and the loop's guardrails — verifier lock, taxonomy,
cheat detector, scaffold self-edit, rollback — behave sensibly on a live run.
Plain-language version: *can the loop make the agent faster at importing the
research CLI without breaking anything, and do the safety checks fire when
they should and stay quiet when they should?*

The problem: `import turing.research.loop.run` cost ~0.57 s, most of it
matplotlib pulled in eagerly through `plots`. Unsolved on `main` at commit
`5febc2a`.

### Configuration
- Engine: `claude -p` (Claude Code CLI 2.1.260) in a Linux session container,
  running as root with `IS_SANDBOX=1` (the CLI refuses `bypassPermissions` as
  root otherwise).
- Sandbox: clone of `main@5febc2a` at `~/turing-workspace/rsi-import-speedup`.
- Verifier (frozen, sha256-locked, `research/results/rsi-import-speedup-2026-09-04/verify.sh`):
  gate = `tests/test_research/test_loop` green against the sandbox `src/`;
  score = median import wall-time of a pristine reference clone ÷ median of the
  sandbox, 7 interleaved pairs so machine load hits both equally. Higher is
  better; 1.0 = no change. Reference-vs-itself scored 0.98 before the run, so
  the measurement noise is ~2%.
- Rounds: 4 · self-edit every 2 rounds · self-edit budget 1 · noise floor auto
  (population stdev of prior scores) · round cap 1800 s · verifier cap 900 s.
- Artifacts: `research/results/rsi-import-speedup-2026-09-04/` (trajectory,
  metrics, taxonomy, verifier lock, final `SCAFFOLD.md`, the agent's
  `NOTES.md`, and the sandbox `src/` diff as a patch).

### Results

| Round | Measured score | Categories | Engine wall | What the agent changed |
|---|---|---|---|---|
| 1 | 2.64× | `cheat_detected` → **void** | 11.7 min | matplotlib deferred to first render in `plots.py` |
| 2 | 4.39× | — | 19.6 min | PEP 562 lazy re-exports in `loop/` and `solver/` `__init__`; pydantic-settings deferred |
| 3 | 4.58× | — | 28.6 min | same for `problems/`; `shutil` deferred in three modules |
| — | self-edit kept, `SCAFFOLD.md` +96 lines, sha `cea62be` | | 1.2 min | distilled "what worked" + a rule to prove a floor before claiming one |
| 4 | 4.77× | — | 14.4 min | `aiosqlite` deferred in `solver/checkpoints.py` |

Every round passed the frozen gate (640 tests, 1 skip). The verifier took
~116 s each time. Best measured score **4.77×** (0.57 s → ~0.12 s). No
rollback: round 4's 4.77 is above the pre-edit best of 4.58 by ~0.19, about
twice the population stdev of the two prior scores (0.094).

**Round 1 is the finding that matters.** The agent's own run of the verifier
reported 2.63; the loop re-measured 2.6427; the cheat detector, built to a
1e-6 relative tolerance, voided a passing round and stopped the loop. That
tolerance was wrong for a wall-clock verifier — two honest runs never agree to
six decimals — and the agent had been *told* it may run the verifier itself.
Fixed in the same PR: the check now fires only when a self-report *exceeds*
the measured score by more than 5%; under-reporting never fires. The measured
score was, and remains, the only one recorded. The void round stays in the
trajectory as written.

**What the self-edit did, honestly.** Round 4 ran under the edited scaffold
and scored highest, but it also landed a new code change, so the +0.19 is
attributable to the code, not the scaffold. One round after one self-edit
says nothing about whether the scaffold edit helped; that would need the
practice/held-out split and the noise floor this loop does not have. The
edit itself reads well — it distilled the three working techniques and added
"positively confirm a floor with grep before claiming it", which the agent
then followed in round 4 — but "reads well" is not a measurement.

### Falsification
Refuting results, checked: a verifier failure on any round (none), a
tampered lock (none — sha re-checked before and after every round), a
sandbox escape or unexpected results write (none), a self-edit touching
anything but `SCAFFOLD.md` (none — `git status` after the step showed only
that file), a rollback (none — none was warranted by the rule). The one
guardrail that *did* fire, fired wrongly, and the fix is above. Not checked:
whether the 4.77× survives on the operator's Mac (the reference and sandbox
were both measured in the same Linux container).

### Estimate vs actual
| | Estimate | Actual |
|---|---|---|
| Wall-clock, 4 rounds | ~60 min | 79 min of loop time (17:08–18:27 UTC) plus one restart after the round-1 void |
| Subscription consumption | not estimated | not metered by this loop; four `claude -p` rounds of 12–29 min each plus a 70 s self-edit |
| Solve rate | expected ≥1.5× on round 1 | every round passed; 2.64× → 4.77× |

### Next
- The sandbox patch (10 files, +470/−196) is a real improvement to `main` but
  is **not** merged by PR #413: it is agent-authored, touches package
  `__init__` re-exports, and deserves its own review. It is archived as
  `sandbox-src.patch` in the results directory.
- This loop still lacks what would make a scaffold-gain claim meaningful: a
  held-out problem and a measured noise floor. Those are loop-1 items and this
  entry does not move them.

---

## 2026-08-12 — Adversarial review of the loop-1 scaffolding; three loop-1 blockers found in "done" prerequisites

**This is a correction entry, not an experiment.** It amends the entry below it —
per the standing rule, by addition and never by edit. No run was executed and
nothing was measured.

**What happened.** The `research/` scaffolding, ADR 0011, the operator guide and
the two setup scripts were read adversarially against the brief and against the
system invariants (frozen verifier · no self-quit · no advice channel · no
cross-type averaging · no held-out leakage · cap enforcement). Twelve findings;
the ones that matter share a shape worth naming, because it will recur:

> **An artifact existed, so the prerequisite read as done, while the property the
> artifact was supposed to guarantee did not hold.**

**Three loop-1 blockers that were not on the prerequisite list** (now
`OPEN-QUESTIONS.md` R1–R3, ADR 0011's open-questions table):

- **R2 — the sandbox is provisioned and never used.** `setup-research-sandbox.sh`
  creates `/Users/turing/turing-workspace` mode 700; the loop's workspace root
  defaults to `Path.home() / "turing-workspace"`, which is the *operator's* home
  when the loop runs as the operator, and nothing anywhere drops privileges to
  `turing`. Following the guide exactly produced a green `--verify` over a
  boundary the agent never goes through — agent-authored Python running as the
  operator, with reach into the vault, `.ssh` and `.claude`. That is precisely
  the failure the account exists to prevent, reported as clean.
- **R3 — escalation, the agent's only exit, has no working transport.** Both ntfy
  settings default to `None` and the client treats that as a *silent* no-op; the
  server is self-hosted on the Surface coordinator, which the same ADR lists as
  dormant. An unattended round today escalates into silence and then waits
  forever, because the agent may not quit. Interim posture: poll the decision
  inbox with the CLI rather than waiting to be paged.
- **R1 — the write surface was recorded two ways in one ADR section.** "The whole
  Turing repo plus `turing-skills` are writable" and "self-edits reach skills,
  never Turing's source" cannot both hold, and the wide reading voids three
  invariants at once, because this repo contains the frozen-verifier
  enforcement, the cap accounting, and the held-out score cells. ADR 0011 §6 now
  takes the narrow reading as operative — free during loop 1 — and leaves the
  widening as an explicit operator decision that has to arrive with a mechanism.

**Correction to the entry below.** That entry restates, one paragraph apart, both
*"H2 is refuted by Δ within noise for the first two self-editing rounds"* and
*"stop when marginal gain < noise floor for **1** round"*. **Those do not
compose.** If round 1 lands below the noise floor — the brief's own most-likely
outcome — the loop stops and round 2 never runs, so H2 can be neither confirmed
nor refuted by its own falsifier. Both numbers are the brief's and both are
faithfully copied; the error was restating them as though they were consistent.
Logged as **R5**, to be settled *before* round 1, since changing a pre-committed
stopping rule after seeing data is not a resolution.

**Also corrected, all in the same direction — claims stronger than the code:**
the verifier freeze is two one-shot checks, not a structural guarantee (a
post-hoc `__setattr__` assignment defeats both, and the mutated verifier re-binds
silently); "`ABANDONED` requires an operator verdict" and "cap extensions only
grow" are conventions honoured at their call sites, not properties `Attempt`
enforces — `evolve` forwards arbitrary field replacement, so `state=ABANDONED`,
a wholesale `cap` swap, and `consumed=CapConsumption()` all get through. None is
reachable by the agent during loop 1, where every call site is code under
review. All four are listed in ADR 0011 §17 as first-self-edit gates, because
that is the moment the "under review" premise stops holding.

**Estimate vs actual:** n/a — no run.

**Decision:** ADR 0011 gains §16 (the self-edit information channel, a settled
brief decision the ADR had omitted) and §17 (contract hardening before the first
self-edit); the frozen error taxonomy moves from a loop-2 item to a
**before-round-0** item, because round 0's failures must be categorised the way
round 1's are or the two rounds are not comparable. `setup-research-sandbox.sh`
now provisions a shared exchange directory — the two scripts' defaults were
mutually incompatible, since tightening the operator home to 750 hides the
agent's own scaffold fork from the agent — and `--verify` asserts the workspace
wiring rather than only the account's existence.

**Not verified:** everything, still. Nothing here was run; these are reading
findings plus four short probes against `contracts.py` in a scratch interpreter.

---

## 2026-08-12 — Pivot: Turing becomes an autonomous research agent

**This is a decision entry, not an experiment.** It is logged here because the
trajectory that follows is only interpretable against the design it was measured
under, and because `JOURNAL.md` is the thread — the thread starts before the
first number.

Source of record: `research/briefs/2026-08-12-autonomous-research-agent.md`
(status `agreed`, agreed after a restatement pass that drew no corrections).
Decision record: `docs/adr/0011-autonomous-research-agent-retarget.md`.

**What changed.** Turing stops being "fine-tune four Jetsons into AI/ML
mini-experts" (ADR 0009) and becomes a general research agent: given a problem,
it works autonomously until the result passes a human-supplied verifier, and it
edits its own scaffold between projects to get better at doing so.

**Question.** Does an agent that rewrites its own scaffold between projects
improve its solve rate on a held-out corpus of verifier-bearing ML projects — by
how much per round, at what cost, and where does it stop?

**Hypotheses.** H1 round-0 solve rate lands in the difficulty band (neither ~0
nor ~1) · H2 self-editing beats round 0 by more than the seed-noise floor ·
H3 gains are capability, not corpus-specific overfitting · H4 human-gate load
trends to zero.

**Falsifiers, pre-committed.** H1 closes at round 0 on a floored or ceilinged
result. H2 is refuted by Δ within noise for the first two self-editing rounds.
H3 is refuted by the primary climbing while the secondary axis flatlines or
drops. H4 is refuted by escalations per round rising.

**Kill conditions** (any one ends the project): self-edits never beat the
frozen-scaffold baseline beyond the noise floor · gains trace to reward hacking
rather than capability · round-0 solve rate near zero, so there is no signal.

**Explicitly not fatal: saturation at round 1–2.** That is what ADR 0009's own
evidence base predicts (ReST-EM 2312.06585, Self-Rewarding LMs 2401.10020). The
deliverable in that case is *where* it saturates, why, and what moves that point.
Recorded because the earlier framing marked it fatal, which was a pre-commitment
to abandon on the most likely outcome.

**Stopping criterion, pre-committed** (brief § Stopping Criterion). Stop when
any of: marginal gain < noise floor for **1** round · the cheat detector fires ·
the secondary axis drops below its round-0 level by more than its own noise · a
constraint gate fails · round budget **R = 3** reached. Subscription exhaustion
**pauses**; it does not stop. Accepted and recorded risk: a single below-noise
round may itself be noise, so this rule will sometimes call saturation early —
tolerable *only* because saturation is a finding rather than a kill condition.

**Load-bearing invariant.** A project is a `(goal, verifier)` pair and the
verifier is **frozen** from the moment it is supplied or approved. The agent may
never edit, relax, or regenerate the bar it is graded on. The agent builds its
own validation splits and diagnostics freely — none of them may ever count as
the official score. *The agent invents; the operator holds the ruler.*

**Sequencing.** Loop 1 (within-project autonomy, frozen scaffold) is a hard
prerequisite for loop 2 (across-project self-modification). All four of the
riskiest prerequisites — enforced harness isolation, the cheat detector,
rollback, the frozen error taxonomy — are loop-2 items, and a measured round 0
is reachable without building any of them.

**Measurement:** four numbers per round, per `driving-functions.md` — continuous
primary score (per problem type and split, **never averaged across types**),
marginal round gain in units of seed noise, cost per unit gain, and human-gate
load (escalations per round).

**Result:** none. No run has been executed. This entry establishes the thread.

**Estimate vs actual:** n/a — no run.

**Decision:** `research/` scaffolding created (`JOURNAL.md`,
`OPEN-QUESTIONS.md`, `DEAD-ENDS.md`, `results/`), which the brief lists as a
prerequisite that did not yet exist in this repo. ADR 0011 filed superseding
ADR 0009's Phase 0 reward-signal and specialty decisions.

**Raised:** the brief's ten open questions seeded into `OPEN-QUESTIONS.md`, of
which **Q11** (how harness read-only-ness is *enforced*) is a loop-2 blocker and
**Q12** (which 8–12 daily tasks, and what the recorded fixtures contain) is a
loop-1 blocker. **Q10** (which ADRs are superseded, and the number for the new
one) is closed by ADR 0011.

**Not verified:** everything. There is no baseline, no noise floor, and no
corpus locked on disk yet. The headroom figures quoted in the brief for the six
speedup problems were measured on the M4 Pro during profiling, but they are
*problem selection* evidence, not results of this program.
