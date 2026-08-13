# Open Questions — Turing autonomous research agent

A map of what is known and unknown, not a to-do list. Answered questions **stay**,
checked, with the run or document that settled them.

Seeded 2026-08-12 from the Open Questions table in
`research/briefs/2026-08-12-autonomous-research-agent.md`. Q-numbers are the
brief's own and are not reused or renumbered — other parts of the brief cite them
by number (Q7, Q11, Q12 in particular).

## From the brief's table

- [ ] **Q2** — Success targets: what solve-rate delta counts as the idea
  working? — raised 2026-08-12, closes at a **design gate, after the noise floor
  exists**. Deliberately not set now: a target picked before the noise floor is a
  target picked without knowing the resolution of the instrument.
- [ ] **Q6b** — What is the Surface coordinator for under the new framing? —
  raised 2026-08-12, closes on an operator decision. Its deployment has been
  stalled on headless multi-node networking since 2026-06-09, and the new framing
  largely routes around that blocker rather than resolving it.
- [ ] **Q7** — Does ROSIE allow outbound internet from **compute** nodes? —
  raised 2026-08-12, closes with one SSH check. **De-prioritised** — it only
  bites when the deferred parallel sweep is queued, and under the agreed plan
  there is no ROSIE requirement at all.
- [ ] **Q9** — Exact MLE-bench figures (task count, Lite size, per-attempt
  budget). Quoted from memory in-interview and **unverified**. — raised
  2026-08-12, closes by reading the paper/repo.
- [x] **Q10** — Which ADRs are superseded, and the number for the new one? —
  raised 2026-08-12, **answered 2026-08-12**: ADR **0011**, superseding ADR
  0009's Phase 0 reward-signal and specialty decisions. See
  `docs/adr/0011-autonomous-research-agent-retarget.md`.
- [ ] **Q11** — How is harness read-only-ness actually *enforced* (separate
  process, container, mount)? — raised 2026-08-12, closes at a design gate.
  **Loop-2 blocker.** Nothing self-edits during loop 1, so there is no harness to
  game yet; this must close before the first self-editing round, not before
  round 0.
- [ ] **Q12** — Which ~8–12 daily tasks form the frozen secondary axis, and what
  do the recorded fixtures contain? — raised 2026-08-12, closes before round 0.
  **Loop-1 blocker.** The set must be frozen *before* round 0 and the inputs must
  be recorded fixtures rather than live state — a Goodhart detector that drifts
  cannot detect Goodhart.
- [ ] **Q13** — Which small model runs on the 8 GB Jetsons for the sub-step tier?
  — raised 2026-08-12, tracked as issue #259, still open. Not on the critical
  path: the Jetsons are idle under the agreed design.
- [ ] **Q14** — Measured tokens/sec: Nemotron 3.5 Lightning MLX-4bit vs
  `muse-glimmer:30b-mlx` on this M4 Pro. — raised 2026-08-12, closes with a
  5-minute `mlx_lm.generate --max-tokens 128` benchmark. Both the ~80–90 tok/s
  expectation and the tool-calling reliability of the recommended sub-step model
  are **architectural inference, not measurement**; the design must not depend on
  either until this is run. Not a loop-1 blocker — the agreed engine is Claude
  only, with no local model in the loop.
- [ ] **Q15** — The frozen error taxonomy for round summaries. — raised
  2026-08-12, **closes before round 0**, not before the first self-edit. The
  brief states both gates (its Prerequisites list files it under loop 2; its
  § The self-edit step calls it design homework that "must exist before round 0
  and stay frozen across rounds"). They are different gates and the looser one
  defeats the stricter one's purpose: round 0 produces the failures the first
  self-edit reads, so if they were categorised ad hoc, round 0 and round 1 are
  not comparable — the exact defect the design-homework note exists to prevent,
  and unfixable afterwards without re-running round 0. ADR 0011 §2 adopts the
  stricter reading; the disagreement itself is logged below as **R4**.

## Raised in the brief outside the table

These are open decisions the brief states in prose. They are listed separately so
the numbered set stays a faithful mirror of the brief's table.

- [ ] **Maestro loophole — does hardcoding count as a solve?** For speedup
  problem #4, an agent could hardcode the `DEFAULT_TRANSIENT_RETRY_*` constants
  into `retry.ts` instead of exporting them from `@maestro/shared`. Both pass the
  gate. The brief flags this explicitly as *"a reward-hacking decision, not a
  detail"* and leaves it undecided. Closes when the corpus is locked — a verifier
  cannot be amended after freezing, so this has to be settled first.
- [ ] **Corpus feasibility criterion — write it before looking at any result.**
  The brief pre-registers the *shape* of the criterion (mechanical, feasibility
  only, e.g. "completes end-to-end on this Mac in under N hours with the baseline
  solver") and the non-negotiable prohibition (never expected score, never "looks
  promising"), but **N is not set**. Closes before Kaggle problem selection.
  If tasks enter because the agent does well on them, every downstream number is
  invalid and no later rigor recovers it.

## Raised after the brief — `R` numbers

Questions the brief did not raise, opened by ADR 0011 or by adversarial review of
it on 2026-08-12. **Numbered `R` rather than `Q` on purpose:** the brief's
Q-numbers are cited by number inside the brief itself, so a new question must not
take the next free Q or the two schemes collide. These are the ADR's table
verbatim, with the evidence that produced them.

The first three are **loop-1 blockers that were not on anyone's list**: each is a
prerequisite that reads as "done" from the outside because an artifact exists,
while the thing the artifact was supposed to guarantee does not hold.

- [ ] **R1** — **Is the agent's write surface `turing-skills` only, or the whole
  Turing repo?** — raised 2026-08-12, closes before the first self-edit. The
  brief states both ("Whole Turing repo + `turing-skills` writable" in Settled
  decisions; "self-edits reach skills, **never Turing's source**" in
  § Multi-agent workflow boundary). This is not doc hygiene: the Turing repo
  holds `contracts.py` (the frozen-verifier enforcement), the cap accounting, and
  `research/results/` (where **held-out** score cells are written), so the wide
  reading lets a self-edit relax its own bar, under-count its own cap, and read
  held-out results off disk. ADR 0011 §6 takes the narrow reading as the
  operative one — it costs loop 1 nothing, since nothing self-edits — and leaves
  the widening as an explicit operator decision that must arrive with a
  mechanism (Q11) rather than by default.
- [ ] **R2** — **The `turing` sandbox is provisioned and not used.** — raised
  2026-08-12, closes before the first unattended run. **Loop-1 blocker.**
  `ResearchLoopSettings.research_workspace_root` defaults to
  `Path.home() / "turing-workspace"`, which is the *operator's* home when the
  loop runs as the operator, and no code under `src/turing/research/` drops
  privileges to `turing`. Setting `TURING_RESEARCH_WORKSPACE_ROOT` is necessary
  and not sufficient — a 700 turing-owned directory is not writable by an
  operator-owned process, and one that could write there would still be running
  as the operator, with full reach into the vault, `.ssh` and `.claude`.
  `setup-research-sandbox.sh --verify` now asserts the wiring and reports "the
  sandbox is sealed", never "the agent is inside it".
- [ ] **R3** — **The escalation channel is unconfigured by default and its server
  is not deployed.** — raised 2026-08-12, closes before the first unattended run.
  **Loop-1 blocker.** `operator_ntfy_topic` and `coordinator_ntfy_base_url` both
  default to `None`, and `NtfyAlertClient` treats either being `None` as a
  *silent* no-op; the ntfy server is self-hosted on the Surface coordinator,
  stalled since 2026-06-09 and listed as dormant by ADR 0011 §7. Escalation is
  the agent's **only** exit, so a dropped push does not degrade to a worse
  outcome — it suspends the loop indefinitely, burning wall-clock, with driving
  function #4 recording an escalation and no resolution. Depends on **Q6b**.
  Interim posture: poll the decision inbox with
  `python -m turing.research.loop.cli --loop-dir … --list`.
- [ ] **R4** — **The brief gates the frozen error taxonomy twice, differently.**
  — raised 2026-08-12, closes before round 0. See Q15 above; recorded separately
  because the resolution taken (the stricter gate) is a decision, and a decision
  taken against an ambiguous source should be visible as one.
- [ ] **R5** — **H2's falsifier is unreachable under the pre-committed stopping
  rule.** — raised 2026-08-12, closes before the first self-editing round. H2 is
  refuted by "Δ within noise for the **first two** self-editing rounds"; the
  stopping criterion stops the loop after **1** below-noise round. If round 1
  lands below the noise floor — which the brief calls the most likely outcome —
  the loop stops and round 2 never runs, so H2 can be neither confirmed nor
  refuted by its own criterion. Both numbers are the brief's. Cheap resolutions:
  restate H2's falsifier at one round, or carve an explicit exception letting
  round 2 run when round 1 is the only below-noise round. Not resolved here —
  changing a pre-committed stopping rule after seeing no data is still changing
  it after the fact, and it must be settled *before* round 1.

## Not questions — recorded so they are not mistaken for open

- **Novelty is unmeasured by design.** The operator wants the agent to push
  boundaries; the brief records that as an aspiration, explicitly not a success
  criterion, because measuring novelty mechanically is unsolved rather than
  deferred. Do not read it into results after the fact.
- **"Real research questions" are deferred to experiment 2**, not open. They have
  no built-in scoring, and sourcing them is open-ended work that would sit on the
  critical path before the noise floor.
