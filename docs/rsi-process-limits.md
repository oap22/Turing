# RSI subprocess process limits

The RSI engine and verifier retain subprocess output through
`turing.research.rsi.engine.run_capped`. The helper owns the process group and
the parent-side stdout/stderr pipes for the lifetime of the call.

## Output cap

`run_capped` accepts the keyword-only `max_output_bytes` argument. It must be a
positive `int`; `bool` is rejected even though `bool` is an `int` subclass.
The default is `DEFAULT_MAX_OUTPUT_BYTES` (8 MiB) for each stream separately.
The retained stdout and stderr byte strings therefore each contain at most the
configured limit. A pipe read can contain more than the remaining capacity,
but only the bytes that fit are appended to the capture buffer.

The first byte beyond either stream's limit sets
`CappedOutput.output_limit_exceeded` and stops the producer's process group
promptly. The result uses `OUTPUT_LIMIT_EXIT` (125), including when the leader
returns zero at the same time. Overflow has result-code precedence over a
wall-clock timeout. If the wall deadline expired before overflow was observed,
`timed_out` can remain true alongside `output_limit_exceeded`; callers use the
overflow flag as the authoritative reason for exit code 125.

The two bytearrays themselves stay within the cap. Peak supervisor memory also
includes bytearray allocation slack, immutable `bytes` result copies (up to
another two caps), the asynchronous transport and chunk overhead, and caller
decoding allocations. This is a conservative per-stream capture bound, not an
exact resident-set-size promise. Memory used by subprocess descendants is
outside the guarantee.

## Cleanup and cancellation

After normal exit, overflow, or timeout, `run_capped` kills the owned process
group and drains both readers for at most the configured grace period. A
descendant that escaped the process group can still hold a pipe; the parent
closes abandoned pipes after the grace and returns bounded output with
`pipe_abandoned=True`. The helper does not claim to kill such escaped
descendants.

Cancellation is handled at every await, including the post-exit drain. The
owned group is killed, both pump tasks are cancelled and joined, and the
parent-side pipes are closed before `CancelledError` is propagated. Reader
exceptions are retrieved during task joining so no background pump continues
to mutate a capture buffer after the caller returns.

An invalid output limit is checked before capture buffers are allocated. Since
the process was already spawned, the helper still kills its owned group,
closes its pipes, and waits for the leader for the bounded cleanup grace before
raising `ContractViolationError`.

## Caller mapping

`ClaudeCliEngine` maps output overflow to `EngineResult(exit_code=125,
timed_out=False)` and appends a short diagnostic that contains no discarded
bytes. `run_verifier` checks overflow before score parsing and returns
`VerifierOutcome(passed=False, exit_code=125, score=None)`, even when retained
output contains a `score=100` line. Its existing `stdout_tail` limit continues
to bound the diagnostic.
