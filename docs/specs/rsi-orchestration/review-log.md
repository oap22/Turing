# Adversarial review record

Baseline inspected: 94f1e1f. Scope: S01, S02, S03 and README.
This records specification review; it is not implementation validation.

## Round 1

- Concurrency/lifecycle: accepted rejoin_after ambiguity. Parent now explicitly
  waits only for original dependencies and designated rejoin nodes; synthesis
  waits for all live nodes. Added an event-controlled partial-rejoin test.
- Concurrency/lifecycle: accepted parent input-alias collision. Require atomic
  rejection before insertion and preservation of existing inputs; added test.
- Test honesty: accepted initial-DAG duplicate output URI gap. Require
  from_dag validation before any dispatch, with public run-path test.
- Test honesty: accepted missing mixed-category prior-pass tests. Require
  live/restart cases for engine_error/no_metrics passes and void precedence.
- Correctness/data loss: no actionable findings; verified append-only history,
  prior-pass replay semantics and explicit readiness gates for future design.

No finding was dismissed as speculative. No unrelated issue was folded into
the implementation slices. A fresh specification verifier checks round-1 fixes.

## Baseline verification

Command from the planning worktree:
`PYTHONPATH=src /Users/owenpacetti/Developer/active/Turing/.venv/bin/python -m pytest tests/test_coordinator/ tests/test_research/test_rsi/ -q`

Result: 720 passed, 3 skipped in 44.23s. This confirms the starting suite;
it does not test the proposed behavior before new regression tests exist.

## Fresh specification verification

Verifier accepted the four revisions, then identified missing same-batch
success/failure and deterministic multi-failure assertions. Added both,
including consumption of every exception and suppression of newly ready work
when any completion in the batch fails. Verifier re-read the revised section:
ready for Luna implementation, no outstanding findings.

S01 dispatched to GPT-5.6 Luna xhigh under #421; S02 under #422, each in its
own baseline worktree. Implementation/code review results follow separately.
