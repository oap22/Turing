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

## S04 specification iteration

Code inspection found unbounded subprocess output buffers in engine._pump.
Added a third bounded implementation slice (#424).

- Concurrency/lifecycle review accepted with one correction: bound retained
  per-stream payload, and account separately for immutable copies, bytearray
  slack and caller Unicode allocations instead of claiming a two-cap RSS bound.
- Test-honesty review found final-length checks could pass after an oversized
  append and later truncation. Added real-capture high-water instrumentation.
- Test-honesty review found timeout-first caller branches could mask overflow
  discovered during cleanup. Added the combined-condition test and explicit
  engine/verifier mapping checks.
- Fresh verifier re-read the corrections and real engine/verifier paths:
  ready for Luna implementation, no outstanding findings. Dispatched #424
  to a third Luna xhigh worker in its own worktree.

## Implementation review and repair

S01 parent review caught and removed an unintended one-deferral-per-parent
limit. The intended contract permits sequential valid deferrals while forbidding
concurrent duplicate attempts; the spec now states that explicitly.

S01 test-honesty review requested three stronger assertions: preserve original
inputs on successful rejoin; observe retrieval of simultaneous exceptions;
prove asynchronous worker cleanup finishes before run returns. Luna repaired these before final verification.

S02 correctness/data-loss review found no runtime defect. Test-honesty review
requested real-loop mixed numeric/scoreless sequences, including restart, so
manual classifier inputs cannot conceal a bookkeeping regression. Luna added the required mixed-transition coverage. Parent independently ran the existing patched RSI suite:
323 passed, 3 skipped in 44.73s; owned ruff/format and RSI mypy passed. Parent
also ran the new scoreless regression against a disposable 94f1e1f archive:
expected failure, observed `[[], [], []]` instead of later `no_progress` rows.

## Final independent verification

- S01: fresh verifier found no outstanding issue, ran 15 new tests, and rejected
  isolated mutations restoring ALL_COMPLETED, removing in-flight exclusion,
  omitting cleanup join, and reversing error order. Parent independently ran
  427 coordinator tests plus owned Ruff/format and coordinator mypy; all passed.
  Parent's baseline copy of the public scheduling test failed because C did not
  start while B remained blocked. Implementation commit 45829c0, PR #425.
- S02: fresh verifier found no correctness issue after the mixed-score test
  additions. Parent's final loop/taxonomy check: 102 passed; Ruff/format clean.
  Existing full-RSI and mypy checks passed as recorded above. Implementation
  commit e1c008a, PR #426.
- S04: independent test-honesty review required both-flags caller mappings and
  observation of a valid score before stderr overflow. Parent required a
  deterministic drain-entry cancellation barrier. Luna repaired all three;
  fresh non-author verifier found no outstanding issue. Parent and verifier
  each ran 327 RSI tests with 4 skips; Ruff/format and mypy passed. Implementation
  commit 5034738, PR #427.
- Parent's separate real-process S04 probe on baseline 94f1e1f returned
  verifier exit 0 / passed true / score 100 and left two readers pending after
  post-exit cancellation. The same probe on the patch returned exit 125 /
  passed false / score None and zero pending readers. Probe retained at
  docs/specs/rsi-orchestration/probes/output_limits.py (run with the baseline
  source checkout on PYTHONPATH and argument baseline, or patched checkout
  and argument fixed).

Each implementation branch incorporates main eebfcfb with no owned-file
conflict. Independent review verdicts are posted on each PR. These are code
correctness checks, not real-model performance experiments or promotion proof.
Repository merge review requirements still apply.

## Combined campaign validation

Parent assembled implementation commits 45829c0, e1c008a and 5034738 on main
eebfcfb in an isolated integration checkout (resulting HEAD 9776a10).

`PYTHONPATH=src python -m pytest tests/ -q -m 'not integration' --ignore=tests/integration`

Result: **3526 passed, 6 skipped**, one pre-existing Starlette/httpx deprecation
warning, in 104.86 seconds. Run outside the managed filesystem sandbox so the
legitimate local socket-binding test could execute. The separate real-broker
integration jobs on all three implementation PRs passed.

Full `ruff check src/ tests/` passed; `ruff format --check src/ tests/` reported
555 files formatted; `mypy src/` passed for 276 source files. No functional
changes were made to main or deployed. Follow the PR review/merge gates.
