# ADR 0007 — Adapter promotion gate: K=1 round-robin canary, REJECTED on regression, hard-examples feedback

- Status: Accepted
- Date: 2026-05-08
- Relates to: ADR 0004 (critic dispatch — feeds critic_score into the offline 2pp gate); ADR 0006 (reward attribution — same `episode_rewards` informs trainer dataset assembly). Slice 4 of "close the learning loop." Implements the gate that decides `AdapterRegistry.promote(STAGED → LIVE)`.

## Context

`coordinator/adapters/registry.py` already defines the
`AdapterRegistry` with `STAGED` and `LIVE` states and a `promote()`
method. The docstring references "Slice 22 / issue #24" as the place
where K-worker live-eval signs off on a STAGED → LIVE flip. Slice 4
implements that signoff.

The PRD pins the surrounding contract:

- Offline gate requires ≥2pp eval improvement on a specialty's held-out
  set before STAGED.
- Staged adapters roll out to one worker first, run eval at "actual
  quantization," then fleet-roll if confirmed.
- Halt + Discord notify on regression.
- Failed-promotion adapters write a "hard examples" batch back to the
  episode store; trainer consumes those as negatives next cycle.

What was unspecified before this ADR:

- K — strict canary or multi-worker test.
- Which worker canaries when a specialty has multiple workers.
- What "live re-eval" actually scores against — the held-out eval set,
  live traffic via critic_score, or both.
- The pass threshold given that quantization typically eats some
  fraction of an offline improvement.
- The state a regressed adapter ends up in, and whether the slot is
  held pending operator action.
- Whether load + eval is one subtask or two on the wire.

This ADR pins all of it.

## Decision

### 1. K=1 strict canary

One worker per specialty pulls the staged adapter and re-runs the
held-out eval set. The other workers in that specialty keep serving
live traffic on the prior LIVE adapter throughout the canary phase.
Pass → fleet rollout (all workers in the specialty load the new
adapter). Fail → see §4.

K>1 was rejected on blast-radius grounds: the canary's job is to
*minimize user-visible disruption during the test window*, and any
shape that puts more workers on the new adapter than the old inverts
that contract. PRD line 46 specifies "one worker first," and PRD is
right.

The redundancy in two-worker specialties (`research-deep`,
`research-summarize`, `research-discover`) is not wasted — the
non-canary worker is the *known-good fallback* serving production
traffic for the duration of the canary.

### 2. Round-robin canary selection

`AdapterRegistry` gains a `last_canary_worker_id: str | None` column
per specialty. When a STAGED adapter enters canary, the gate selects
the *next* worker in the specialty's pool after `last_canary_worker_id`
(by stable lexicographic order); writes that worker_id back as the new
`last_canary_worker_id`.

Deterministic-first (always the alphabetically-first worker) was
rejected: it pins regression history to one worker, distorting any
future per-worker eval-score tracking. Lowest-traffic-at-promotion was
rejected: real-time decision against a moving target with no
deterministic reproduction.

Round-robin is fair, idempotent given prior state, and one column.

### 3. Eval: held-out set at quantization, not live traffic

The canary re-runs the specialty's `evals/<specialty>/*.jsonl` set on
the canary worker, scored by the existing per-specialty scorers
(claim-preservation, citation-correctness, etc., from slices #87/#88).
Compares the canary's quantized score to the prior LIVE adapter's
recorded `canary_eval_score`.

Live-traffic scoring via `critic_score` was rejected:

- Critic scores are themselves model outputs (judge worker per ADR
  0004); judge-model behavior drifts. Using live critic_score as the
  promotion gate creates a circular dependency where a regressing
  judge can mask an adapter regression, or a fine adapter can look bad
  during a judge's bad week.
- B serves bad traffic to users while the gate collects data. The eval
  set is fixed, human-curated, and deterministic — the right
  groundtruth.

Hybrid (eval set first, live shadow second) was rejected as
multi-day-cycle on a weekly-promotion cadence — slows the trust
accumulation loop the PRD wants.

### 4. Pass threshold: ε-tolerant non-regression

```
canary_eval_score >= prior_live_canary_score - 0.5pp
```

The offline gate already verified ≥2pp improvement at trainer
precision. Quantization typically costs 0–2pp of eval performance
depending on the specialty's loss profile. Demanding the *full* 2pp
delta to survive at quantization double-counts the bar; demanding
strict non-regression with no tolerance falsely halts adapters whose
"no real change" looks like jitter on small eval sets.

ε = 0.5pp is "the offline improvement survived quantization within
sampling noise." Tunable in config; the value will be revisited the
first time a specialty's eval set produces variance that demands a
different ε.

Each LIVE adapter records its `canary_eval_score` on promotion. The
next canary compares against that recorded value, not against any
re-derived offline number.

### 5. Wire: single combined `canary_eval` subtask

```python
SubtaskKind.CANARY_EVAL  # new
# payload
{ "adapter_manifest": {...}, "eval_set_path": "evals/<specialty>/" }
# result
{ "status": "scored" | "load_failed" | "eval_failed",
  "score": float | None,
  "failed_cases": [{eval_id, expected, got, ...}, ...],
  "error": str | None }
```

Adapter-load and eval-run are intrinsically coupled: an eval only
matters against a specific loaded adapter; a load only matters insofar
as eval will run on it. Splitting them across two subtasks creates a
two-step state machine the worker has to remember between dispatches —
a worker crash mid-state leaves "loaded an adapter, but for what
reason?" undefined.

Atomic combined subtask: load + score in one execution. Crash mid-eval
retries the whole thing cleanly via the existing `subtask_id`
idempotency.

The generic eval-runner already lives on workers (slices #87/#88).
Per-specialty scorers register against it. No new runner code.

### 6. Regression → REJECTED, permanent, hard-examples feedback

A canary failure (`score < prior - ε`, or `status='load_failed'`, or
`status='eval_failed'`) puts the adapter in a new `AdapterState.REJECTED`.

Properties:

- **Permanent.** A REJECTED adapter cannot re-canary. The training
  pipeline produces a new candidate next cycle.
- **Stateless across promotions.** No held slots, no operator-required
  unlock. Subsequent STAGED adapters for the same specialty proceed
  through the gate normally.
- **Canary reverts** to the prior LIVE adapter immediately on REJECTED
  transition.
- **Hard-examples feedback.** The worst-N failed eval cases (`failed_cases`
  in the result) get written to a `hard_examples` batch on the
  episode store. The trainer reads from this batch when assembling
  the next SFT/DPO dataset, weighting those cases as negatives.

Operator-button-required resolution (B in the grill) was rejected: an
adversarial design where the gate stalls until the operator presses a
button, accumulating training output that cannot canary. The PRD's
"manual approval graduating to autonomy" applies at training-job
launch, not at promotion-gate cleanup.

`QUARANTINED` middle-state (C in the grill) was rejected as
ceremony — either re-evaluate or reject; the middle is decoration.

### 7. Discord notification on regression

```
Adapter `<name>:<version>` failed canary on `<specialty>`.
  Δ score: −1.8pp (canary 71.2 vs. prior 73.0)
  Reverted to `<prior_name>:<prior_version>`.
  Hard-examples batch: <count> cases.
  Regressions this month for `<specialty>`: <N>.
```

The per-specialty regression-rate is the early-warning signal: PRD
line 245 specifies "if win-rate drops below 30% on a specialty, the
dataset/eval is the bug." The operator needs to see clusters of
regressions without dashboard-diving.

Verbose inlining of failed eval cases was rejected as channel-spam.
Operators who want them pull from `hard_examples` directly.

## Consequences

- `AdapterRegistry` schema gains: `last_canary_worker_id` (per
  specialty), `canary_eval_score` (per LIVE adapter row), and a new
  `REJECTED` state on the `AdapterState` enum.
- A new `hard_examples` table on the episode store: `(specialty,
  adapter_version_attempted, eval_id, expected, got, failed_at_ms)`.
  Trainer-side dataset-builder query reads it.
- A new `SubtaskKind.CANARY_EVAL` plus per-specialty payload typing.
  No new transport: same dispatch path slices #93–#97 shipped.
- The Discord bot gains a new notification template. No new event
  types — same task-message channel.
- The promotion cadence becomes self-clearing: STAGED → canary →
  {LIVE, REJECTED} terminal in minutes, no held slots. Next training
  cycle sees a clean slate.
- A regressing specialty surfaces fast (regression-count in the
  Discord message); the operator-side response (re-look at dataset or
  eval) is policy, not gate-enforced.
- Promotion is asymmetric: a passing canary auto-rolls to fleet; a
  failing canary auto-rejects to dead-letter. No middle ground. This
  is the trade-off — speed of safe promotion vs. ability to "save"
  borderline adapters. The trainer is the right place to save them
  (via hard-examples feedback into the next dataset), not the gate.

## Out of scope

- K=2+ canary or staggered fleet rollout (e.g. roll to half on canary
  pass, watch live for an hour, then full fleet). Defer until cluster
  size or risk profile warrants.
- Per-specialty ε tuning. Single global ε in v1.
- Automatic operator-pings on persistent regression patterns (e.g.
  "this is the 5th regression this week, the dataset is broken"). The
  per-message regression-count surface lets the operator notice; an
  alarm is a future opt-in.
- Operator-override CLI to force-promote a REJECTED adapter (escape
  hatch for "the eval set itself is wrong, not the adapter"). Add when
  the operator has wished for it twice.
- Live-traffic shadow eval for confidence-building post-canary. PRD
  doesn't ask for it; the canary at quantization is the gate.
