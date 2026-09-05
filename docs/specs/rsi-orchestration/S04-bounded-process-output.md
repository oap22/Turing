# S04 — Bound RSI subprocess output without silently changing scores

Status: adversarial-reviewed; dispatched to Luna xhigh. Parent #419. Issue #424. Baseline 94f1e1f.

## Failure and contract

engine._pump extends bytearrays until EOF. The process deadline does not bound
memory: a verbose CLI or verifier can exhaust the supervising process before
its deadline. Bound retained stdout/stderr and fail explicitly on overflow.
Never interpret truncated verifier output as a passing scoreless result.

## Ownership and non-goals

Own research/rsi/engine.py, verifier.py, test_engine.py, test_verifier.py and
a new docs/rsi-process-limits.md (all code/test paths retain existing prefixes).
No changes to loop.py, taxonomy.py, CLI, dependencies, persisted record schema,
OS isolation policy or remote execution. No new taxonomy categories: engine
output overflow uses engine_error and verifier overflow uses verifier_failed.
No live-provider runs required. Coordinate with #422's separate loop changes.

## Requirements

- run_capped adds keyword-only max_output_bytes with a documented default of
  8 MiB PER STREAM, positive integer excluding bool. Each retained bytearray
  never exceeds that cap; chunks may be read but excess bytes are not retained.
  Below/at cap, return exact original bytes. Existing callers remain compatible.
- Overflow occurs only on byte cap+1, for stdout OR stderr independently.
  Pumps signal overflow immediately; supervisor kills the owned process group
  promptly, rather than waiting for the original wall timeout. Continue bounded
  pipe cleanup under the existing grace. The cap bounds retained payload per stream. Peak run_capped memory also
  includes bytearray allocation slack, immutable result copies (up to another
  two caps), and transport/chunk overhead. Document a conservative bound, not
  an exact two-cap RSS promise. Caller Unicode decoding adds allocations;
  descendant memory is outside this guarantee.
- Add a backward-compatible CappedOutput.output_limit_exceeded boolean default
  false. On overflow return a named exit-code constant 125, never successful
  exit even when process returncode races to zero. Preserve limited diagnostics.
  timeout flag is true only if the wall deadline independently expired first;
  define deterministic precedence: detected overflow wins result code 125 even
  if timeout is also observed. Keep the overflow flag as the authority.
- ClaudeCliEngine maps flagged overflow to EngineResult(exit_code=125,
  timed_out=false), with a short explicit diagnostic; it must not report CLI
  success. Diagnostic additions may exceed the cap by a small fixed string,
  never include discarded bytes. Keep result types and stdout fields intact.
- run_verifier maps overflow to VerifierOutcome(passed=false, exit_code=125,
  score=None), even if retained bytes contain score=100 and the process exits
  zero. Its existing tail limit still bounds the diagnostic. Check overflow
  BEFORE parsing a score or computing passed; no verifier-score fabrication.
- Parent cancellation at ANY await in run_capped, including post-exit pipe
  draining, kills the group, cancels/joins both readers, closes abandoned pipes,
  and propagates CancelledError. Retrieve reader errors. Do not leave background
  pump tasks extending buffers after the caller returns. Preserve existing
  bounded handling of descendants that leave the process group; do not claim
  to kill escaped descendants. No process-wide/global task cancellation.
- Validate limits before allocating buffers; use clear ContractViolationError.
  This helper receives an already-spawned process: on invalid limit it must
  still clean up the owned group/pipes before raising, so validation cannot
  strand a child. Document this ownership behavior.

## Acceptance evidence

Use real subprocesses in temporary directories through exported paths, with
small max_output_bytes in helper tests so tests allocate kilobytes, not huge
memory. Give test subprocesses hard outer cleanup even when assertions fail.

1. At-cap stdout/stderr returns exact bytes and no overflow; cap+1 stdout
   fails; cap+1 stderr fails; combined streams below individual caps succeed.
2. A process prints beyond cap then waits for a signal indefinitely: run_capped
   returns overflow promptly before its much longer timeout, group is reaped,
   retained lengths bounded. A bounded watchdog prevents hung regressions.
3. Short process writes cap+1 and exits zero: still overflow/125. Alternate
   stdout/stderr bursts exercise simultaneous-reader detection and boundedness.
4. Real ClaudeCliEngine using a temporary executable that overflows the default
   cap returns 125, timed_out=false. No mocked engine result or parser-only test.
5. Real run_verifier overflows while printing a valid score both before and
   after the crossing, exits zero: passed=false/score=None/125. Repeat with
   stderr-only overflow and valid small stdout score. A below-cap verifier still
   uses its last valid score line exactly as before.
6. Cancel during process wait AND after parent exit while an escaped child
   holds a pipe; cancellation reaches caller, owned reader tasks are done,
   buffers stop growing, test explicitly cleans the escaped child. Use barriers
   or a controlled real wait boundary, not a timing lottery.
7. Instrument the actual capture buffer allocation/mutation via a narrow test
   hook or instrumented bytearray in a real run_capped invocation. A read chunk
   crossing the small cap by many bytes must never make buffer high-water
   length exceed its cap. Final output slicing alone is not sufficient proof.
8. Control run_capped so deadline expires then buffered overflow is observed
   during cleanup: both flags must be true and exit 125. Supplemental caller
   mapping fixtures with both flags true must produce EngineResult(125,false)
   and VerifierOutcome(125,false,None), in addition to real detection tests.
9. Zero/negative/bool limits raise and leave no owned child/pump. Existing
   timeout, normal exit, pipe abandonment, verifier parsing tests stay green.

Prove overflow and post-exit cancellation regressions fail on baseline in
scratch and pass after patch. Run full RSI tests; ruff check/format and mypy on
owned modules. This is resource-bound correctness evidence, not a measured
improvement in task-solving ability. Review tier follows final diff size and
standard repository rules; no automatic merge or deployment.
