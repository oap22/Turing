# Dead Ends — Turing autonomous research agent

Grep-optimised. One line per ruled-out thing, so a future session can check in
seconds whether something has already been tried.

Format: **what was tried** · why it failed · run ID · **what would make it worth
revisiting**. That last clause is what separates a dead end from a permanent
prohibition.

## Ruled out

*(empty)*

**This file is empty on purpose, and the emptiness is itself a finding.**

The operator has attempted nothing in this direction before — 2026-08-12 is the
first attempt (brief § Priors & Dead Ends). Two consequences follow, and both
change how the rest of the record should be read:

1. **Every prior in the brief is borrowed from the literature, none from the
   operator's own experience.** "Expect 1–3 useful rounds then saturation",
   "ReST-EM regressed on coding after iteration 1", "accumulate never replace" —
   these are other people's measurements on other people's loops. They are
   reasons to *expect* something, not evidence about *this* system. Treat them as
   hypotheses inherited on credit, and record estimate vs actual from the very
   first run so real priors start accumulating.
2. **The bench cycle is worth more, not less.** With no personal intuition for
   how this class of loop behaves, the synthetic end-to-end pass — every seam
   real, edges canned, second round rigged to fail so the rollback chain is
   proven before anything real is at stake — is the only cheap source of
   experience available.

## Scope note — what belongs here

Directions and approaches ruled out **by a measured run of this program**, each
citing its run ID under `research/results/`.

Two things deliberately do **not** get copied here:

- **Corpus-candidate disqualifications** from the 2026-08-12 headroom profiling
  (webui and TUI having zero candidates, the NCA training loop being effectively
  already fast, the NCA recovery sweep's gate being broken by construction, the
  vault-watcher cold-start duplicating problem #5, the Maestro full suite being
  96% one file). Those are results about *problem selection*, they are recorded
  in full in the brief's headroom-profiling section, and duplicating them here
  would create a second copy that drifts. They migrate here only if one of them
  is ever re-litigated as a direction.
- **Decisions superseded by the pivot** (the local LoRA flywheel, the generalist
  adapter, dollar metering, the Jetson-worker premise). Those were not tried and
  found wanting; they were retargeted. They live in ADR 0011 § Superseded.
