# S02 — Honest pass/fail RSI progress across resumes

Status: adversarial-reviewed; dispatched to Luna xhigh. Parent: #419. Baseline: 94f1e1f. Implementation issue: #422.

## Problem and contract

The real loop passes best_score=None forever on a verifier that reports only
pass/fail. classify_round therefore labels every passing round as improvement.
Track whether a valid verifier pass has previously occurred independently of
numeric best_score. First valid pass is progress; later scoreless passes are
no_progress, including after restart. Persisted facts remain append-only.

## Ownership and boundaries

Own src/turing/research/rsi/taxonomy.py, loop.py, their test modules, and the
corresponding statements in docs/rsi-loop.md. No enum additions, digest/version
changes, RoundRecord schema changes, CLI changes, rollout/rollback policy
changes, self-edit budget changes, dependency changes, or engine adapters.
Numeric scoring remains higher-is-better with its current comparison behavior.
This deliberately does not reclassify engine-failure rounds or change whether
the verifier can validate useful work after the engine exits nonzero.

## Exact semantics

A prior valid pass is a record with passed=true and void=false. It may carry
no_metrics or engine_error: verifier acceptance is the existing authority for
artifact correctness. Keep this rule identical in live updates and replay.

Add a backward-compatible keyword-only prior-pass signal to classify_round;
when omitted, preserve the public function's legacy inference from best_score.
The running loop must always pass the explicit signal. Do not manufacture a
numeric score such as 0/1 and do not use truthiness of zero or negative scores.

For a current passing verifier with score=None, classify no_progress iff a
prior valid pass exists. Failed or void earlier records do not establish that
state. Once established it remains true after later failures. Numeric rounds
still use existing best_score/previous_score rules. A previous numeric valid
pass counts as a prior pass for a later scoreless round. A previous scoreless
pass does not invent a numeric baseline for a later numeric round.

Read historical rows as facts to reconstruct state; never rewrite old rows or
pretend their past categories were repaired. The taxonomy enum digest freezes
category names, not historical classifier implementation. Update docs to say
new rows have corrected behavior while old category counts retain old labels.
Record the baseline/implementation revision in the PR; a future experiment
manifest may version classifier semantics without retroactively mutating runs.

## Acceptance tests

- Exercise RsiLoop.run using FakeEngine plus a real scoreless verifier in a
  disposable sandbox: pass, pass -> first no no_progress, second no_progress.
  Include metrics so absence of telemetry cannot explain the category.
- One pass in invocation 1, restart with a newly constructed loop, then pass:
  new row has no_progress; old trajectory bytes remain the exact prefix.
- Fail, pass, pass: first success is progress, next is no_progress.
- Pass, fail, pass: final pass is no_progress. Do not reset state on failure.
- Replay a void passing row without prior valid passes: it does not establish
  success. Since a cheat stops real runs, test replay state directly plus a
  legal first run; do not bypass a tamper stop to fabricate resumed success.
- Explicit classifier signal false plus best_score=None; explicit true with
  best_score=None; omitted signal preserves legacy calls.
- Parameterize live and fresh-loop restart tests over prior non-void passes
  carrying engine_error and no_metrics: later pass remains no_progress.
  Include those flags on a void replay record: void takes precedence.
- Numeric zero/negative/equal/improving scores retain existing behavior; mixed
  scored/scoreless cases follow the exact rules above. Frozen digest unchanged.

Run pytest tests/test_research/test_rsi/ -q, ruff check/format --check on owned
files, and mypy src/turing/research/rsi/. Prove the scoreless end-to-end test
fails on the baseline in scratch and passes on the implementation.
No claim that this improves task success: it repairs the signal fed to learning.
Review tier: fresh agent review unless implementation scope triggers human tier.
