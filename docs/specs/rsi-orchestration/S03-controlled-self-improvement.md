# S03 — Controlled orchestration self-improvement

Status: design for follow-on implementation; not an executable experiment.
Parent: #419. Depends on accepted S01/S02 evidence. Baseline: 94f1e1f.

## Question and falsifier

Can a candidate orchestration policy increase verified task completion at a
fixed resource budget on unseen DAGs, without increasing lost/duplicate work?
Falsify the candidate if it fails any lifecycle invariant, cannot beat the
incumbent on the predeclared endpoint, or the comparison is inconclusive.
Retain the incumbent on inconclusive evidence. Do not infer causal gain by
comparing the best score before a scaffold edit to the best score afterward.

## Proposed architecture

A supervisor owns manifests, evaluators, limits, seeds, and promotion records.
An implementer can propose a bounded patch or typed policy and a hypothesis;
it cannot edit the scorer, baseline checkout, holdout, or budget authority.
Evaluate candidate and incumbent from the same immutable source/task snapshots
in separate writable sandboxes, resetting task state between attempts. Run
both arms on matched task/seed/model configurations; alternate arm order.
Pin dependency environment and model settings; record provider model identity
and tool versions. Never call a mutable provider alias fully reproducible.

Phase A uses deterministic scripted workers through DAGOrchestrator.run:
chains, wide fan-out, uneven branches, diamond fan-in, dynamic fragments,
collisions, failure, cancellation, and remote timeout/retry. These verify
behavior and collect dispatch order and work counts; they do not estimate LLM
quality. S01's event-controlled tests become the first counterexamples.
Phase B uses frozen checkable coding tasks and real implementers at an agreed
money/token/wall-clock cap, only after Phase A and a research-loop brief pass.
Keep evaluation orchestration external to candidate code; a modified observer
inside a candidate cannot attest to its own performance.

## Evidence schema and authority

A supervisor-owned manifest includes schema_version, run_id, baseline_sha,
candidate_sha, spec_sha, evaluator_sha, corpus_version, split, task IDs,
seed list, provider/model/reasoning, environment digest, allowed mutation paths,
round/attempt/token/dollar/wall-clock caps, endpoint, margin, stopping rule,
and authorized promotion scope. Validate all before invoking an engine.

Per attempt retain arm, task ID, seed, attempt ID, input/candidate/evaluator
hashes, start/end, terminal outcome, verified correctness, work count, usage
source, measured usage, and raw verifier artifact digest. Missing usage is
unknown, never zero. A reserve charged before dispatch covers the worst allowed
attempt; settle actual usage afterward, retaining reserve when usage is unknown.
All retries, failures, timeouts, reviews and abandoned attempts consume budget.
Budget state and attempt identity survive restart; ambiguous dispatched attempts
are reconciled or terminal unknown, never silently dispatched twice.

Use an append-only supervisor journal with exclusive ownership. On recovery,
replay complete records, explicitly flag a torn tail and ambiguous in-flight
attempts, and refuse concurrent owners. Do not treat mutable agent-produced
metrics.jsonl or trajectory content as authoritative promotion evidence.
Desktop metrics are a derived view preserving existing pane contracts.

## Acceptance and promotion policy

Freeze the primary endpoint and minimum meaningful effect before sampling.
Primary for Phase B: paired difference in verified completions at equal caps.
Report paired uncertainty and disaggregated failures, tokens and wall time.
Do not collapse correctness, latency and cost into an arbitrary weighted score.
A preregistered one-shot holdout decision follows development selection; budget
and multiplicity correction must be specified if more than one candidate is
submitted. Failures remain in the denominator. No optional stopping on a lucky
positive result. Each deterministic safety/lifecycle scenario is a hard gate.

A pass yields a reviewable candidate PR plus evidence manifest, not a live edit.
Promotion requires exact candidate/evaluator hashes, independent adversarial
review and the repository's merge tier. Holdout details stay outside candidate
write/read capabilities during selection; publishing counterexamples consumes
that holdout and requires a fresh split for subsequent comparisons. A rollout
starts with a bounded canary; rollback restores the prior policy and preserves
all observations. Canary criteria and authority require their own concrete spec.

## Implementation decomposition and readiness gate

S03a: immutable manifest and single-owner durable attempt/budget journal.
S03b: deterministic paired harness calling the real orchestrator, with exported
trace comparison and a CLI that only plans unless a validated manifest exists.
S03c: isolated real-model paired evaluator and statistical decision report.
S03d: candidate proposal -> review -> exact-hash promotion/canary integration.
Each receives an issue, narrow ownership, schema/version decisions and explicit
acceptance tests before Luna implementation. Do not implement this entire
architecture from this document: contracts for enforcement/OS isolation,
provider accounting and experiment sample size remain to be selected.

Required failure injections for those specs: crash after reserve/before launch;
crash after launch/before result; duplicate completion; stale supervisor lease;
changed evaluator/dependency; unknown cost; delayed remote result; cancellation;
agent rewrites evidence; same task leaked across splits; a candidate memorizes
development fixtures; a noisy lucky candidate loses on the sealed holdout.
No real-model improvement claim is valid until the corresponding experiment is
run through the research-loop workflow with its approved budget and falsifier.
