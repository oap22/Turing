# Agent communication: review and verification

Tracking: https://github.com/oap22/Turing/issues/436

## Scope and feasibility outcome

Implemented a provider-neutral, same-machine channel: a durable SQLite mailbox,
JSON CLI, and a native Turing tool. No additional dependencies or network service
are required. Shell-capable coding harnesses use the CLI; tool-capable local
models can use the native tool through an explicit routing opt-in.

No automatic model wakeup, inter-machine transport, MCP-specific adapter, or
authentication between processes sharing the database is claimed. The desktop's
agent panes remain transcript observers. The shared multi-agent workflow skill
now tells configured coding agents how to register, poll, reply, and acknowledge.

## Adversarial review

Luna xhigh reviewers independently attacked persistence and concurrency. A third reviewer independently checked security in the native
integration. Reviewers use exported code paths
and scratch repros, do not modify the implementation, and must supply a concrete
starting state, action, and wrong result.

Confirmed core findings from the first round:

- Existing application databases containing only SQLite metadata could be adopted.
- A colliding or incomplete mailbox schema marker could cause missing tables to
  be created instead of rejecting the database.
- Fractional schema versions were truncated to the supported integer version.
  Both independent reviewers found this defect.
- Duplicate JSON object keys in CLI input were silently reduced to the last value.
- In-memory transaction rollback could race another thread after an error.

The hardening contract is to reject non-mailbox databases and incomplete schemas
without changing them, require the exact supported integer schema version, reject
ambiguous JSON input, and support durable files only. In-memory mode is removed.

The hardening changes are implemented. The parent reran the independent probe:
all nine rejection checks pass, including byte-for-byte preservation of rejected
files. The normal subprocess exchange and all 23 core/native-mailbox tests also
pass after the fixes.

The native security review is clean: fixed identity, inert message content,
local-only failure behavior, opt-in/default routing, and valid tool-result
association were exercised. A malformed transcript with missing prior tool
results was discarded because the real Agent loop does not produce it.

The fresh verifier found two additional defects: an otherwise complete schema
with an extra CHECK constraint was accepted but failed on its first send, and
native configuration resolved `:memory:` into a disk filename before validation.
The second hardening round compares full owned schema DDL with canonical v1 and
rejects the memory sentinel before path resolution. Both regressions failed
before their fixes and pass afterward. Original-v1 compatibility is preserved.
The parent reran all nine rejection probes, the independent concurrent CLI
exchange, and 69 config/core/native tests successfully.

Review status: final independent verification found no additional defects.
The fresh verifier reran the original nine-case probe, rejected unknown schema
constraints without changing database bytes, reopened a real database created by
the original v1 implementation, and verified inbox/ack on its existing message.
Twelve simultaneous initializer processes and twelve same-key sender processes
passed. Environment, Path, and startup-helper memory-sentinel probes all reject.
Non-author human/code-owner review remains required before merge.

## Validation evidence

After the final hardening fixes, the CI-equivalent Python suite passed:
**3,651 passed, 6 skipped, 92.84% coverage** (required minimum: 75%).
The command was `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null .venv/bin/pytest tests/ -q -m "not integration" --ignore=tests/integration --cov=turing --cov-report=term --cov-fail-under=75`.
Docker integration tests were excluded from this local run.
The initial sandbox-only run could not bind a loopback socket in one existing
trainer test; allowing loopback access resolved that environment failure.
Ruff and mypy over all source files passed (281 source files checked by mypy).
A wheel build and an installed `turing-agent-mailbox register` invocation in a
temporary environment also passed.

A separate parent-run subprocess smoke test exercised registration, nested JSON,
two-way replies, restart persistence, repeat polling, acknowledgment ownership,
workflow isolation, retry conflicts, and 24 concurrent sends. Independent
reviewers also checked 30 same-key sender processes (one persisted message),
50 concurrent initializations, full workflow capacity, and crash persistence.

A live local model, `muse-glimmer:30b-mlx`, completed `peers` then `send` through
LLMRouter in local-only mode, the real executor/safety gate as a non-admin, and
a scratch SQLite mailbox. It received the successful tool result and reported
the stored message ID. Only the mailbox tool was offered; no cloud call occurred.
This demonstrates the installed model's tested path, not universal model compliance.

The multi-tool Agent-loop regression was also run with the original agent-loop
implementation in a disposable source copy: it failed because two assistant
batches were recorded instead of one. The same test passes with the corrected
loop. During rebase, current main already contained the equivalent fix; this
feature preserves that upstream implementation and adds the integration test.
