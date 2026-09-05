# RSI for Turing orchestration

Owner: oap22. Tracking: #419. Baseline: main `94f1e1f` (2026-09-05).
Implementation model: GPT-5.6 Luna, xhigh reasoning. Status: S01/S02/S04 implemented and independently reviewed; S03 follow-on design.

The goal is reliable completion per unit cost, not more agents, more rounds,
or higher self-reported scores. Existing RSI can edit SCAFFOLD.md, but a best
score before/after comparison mixes scaffold effects with accumulated code,
randomness, and task difficulty. It is a regression heuristic, not evidence
that self-improvement works. #413 is merged; its live speedup trace does not
establish causality for scaffold edits. The research runner's self_edit_seam
is explicitly unwired. Do not silently join these two systems.

## Delivered implementation PRs

| Spec | PR | Verified behavior |
| --- | --- | --- |
| S01 | [#425](https://github.com/oap22/Turing/pull/425) | Eligible dependents run without waiting for unrelated tasks; cleanup is joined |
| S02 | [#426](https://github.com/oap22/Turing/pull/426) | Prior valid scoreless passes remain no_progress across restarts |
| S04 | [#427](https://github.com/oap22/Turing/pull/427) | Output is capped per stream; overflow fails explicitly; cancelled readers are joined |

All three were implemented by GPT-5.6 Luna with xhigh reasoning, then reviewed
and verified independently. Read review-log.md for before/after evidence and
review repairs. These changes are PRs, not deployed runtime changes. S03 remains
a follow-on experiment design, not evidence that scaffold edits improve models.

## Ordered campaign

1. S01: remove the coordinator's wave barrier, with deterministic scheduling
   regression tests. This changes real agent orchestration.
2. S02: repair pass/fail RSI progress accounting, including restart behavior.
   This makes the feedback given to self-edit honest without changing policy.
3. S04: bound engine/verifier output and make overflow an explicit failure.
4. S03: specify a controlled orchestration evaluation and promotion workflow.
   Implement the first two before adding new autonomous mutation surfaces.

S01, S02 and S04 own disjoint code files and may be implemented concurrently. S03 is a
follow-on design, not permission to launch paid model experiments or promote
untested candidates. No automatic merge/deployment. Every implementation PR
states its review tier and references its issue. No edits to active #415's
agent/core.py, memory/store.py or remote dispatch optimization work without
checking that PR's final ownership first.

## Delivery contract

Each Luna worker receives the complete spec, baseline SHA, issue, owned files,
non-goals, and test commands. It creates its own issue-named worktree, records
fail-before/pass-after evidence, and reports the actual diff and commands.
A different agent reviews the code with an assigned adversarial lens; a fresh
verifier attempts to disprove fixes. The orchestrator independently reruns
focused checks before reporting success. Specs are reviewed too: a finding
must give starting state -> action -> wrong result, and is resolved in writing.

## Further ideas, prioritized by what the first results reveal

- Durable run budgets and single-writer fencing across resumes: prevent a
  restart from resetting spend or two supervisors from owning one loop.
- Counterexample bank: retain minimized failing DAGs by behavior family;
  reserve an unseen holdout so repeated tuning cannot overfit the bank.
- Typed candidate policies: bounded concurrency, retry allocation, routing,
  and context selection; evaluator and budget authority stay supervisor-owned.
- Value-of-information escalation: spend on a stronger reviewer only when
  disagreement or missing evidence predicts failure; compare against fixed
  escalation at equal budgets before adoption.
- Context distillation: carry verified outcomes and unresolved constraints
  between attempts, retaining provenance and measuring lost constraints.
- Stopping policy: prefer the incumbent after inconclusive comparisons;
  charge failures and abandoned attempts so extra retries cannot fake gain.

These are hypotheses, not measured improvements. An idea advances only with
a bounded spec, a falsifier, independent review, and a runnable acceptance test.
