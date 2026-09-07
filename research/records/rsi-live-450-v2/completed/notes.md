# Turing live RSI source improvement

Question: can a real Codex/Luna RSI round repair trajectory evidence corruption in Turing?

Observed verifier passes: 1; process exit: 0. Independent post-run review and holdout probes are still required. This is a bounded engineering test, not evidence of causal scaffold self-improvement.

## Verification

Supervisor completed one round, exit 0, passed true, void false, score 1. The source-selected frozen verifier passed; independent review ran the candidate trajectory tests (5 passed) and reviewed normal, special-file, late-write and legitimate supervisor event handling. The immutable baseline fails six of eight external cases; the seeded partial candidate fails only the late verifier-write case. Final integrated branch passes 386 tests plus 4 skipped, including all eight external cases. The evaluator asserts imported module locations inside pytest, and its four manifest hashes were independently checked. The launcher rejects no-new-round, event-only and pass-then-fail outcomes.

This corrects the invalid v1 verifier import path; v1 is preserved as an interrupted negative run. This is evidence of a specific Turing code improvement produced by a real Codex Luna xhigh round, not causal evidence that scaffold self-editing improves agent quality. v2 self-edit-every is 0; recursive proposal, judgment, rollback and resume behavior are covered by deterministic tests.

Provenance: engine 1cc5b07b6a6ddaaf02b0b0996f9efe7ca70643c7 (clean at launch), candidate b8d67205615a7cea63f9431e8e7a0c1e32dc8388 (no source diff), evaluator manifest 6fc2b5ca1880fa413e9ef6872a3ed6d18a1d73d9bcb743c05e468c0477bd4c20. Runtime /Users/owenpacetti/Developer/active/Turing/.venv/bin/python (Python 3.11), native shell Codex /Users/owenpacetti/.npm-global/bin/codex 0.153.0; model gpt-5.6-luna, reasoning xhigh, workspace-write. Provider seed and marginal subscription dollar cost unavailable. Estimate 12 minutes; actual 440.672 seconds. Native Turing launch and new engine/verifier configuration inspected using computer use.

## Final integration review

The final scaffold-proposal guard restores trusted trajectory bytes even when propose() raises after mutation. Independent two-invocation verification retained legitimate score 1 and rejected the offending edit. Integrated source 7d409c9 passed 389 RSI/evaluator tests with four skips. An unchanged 300 ms Claude timeout test failed once under concurrent load because child.pid had not appeared; it passed unchanged alone and in the quiet final confirmation. Both failure and confirmation are preserved in the repository report. Broad Python run: 3644 passes, five skips, two socket-bind failures; both socket tests passed unchanged with loopback access. Full mypy: 277 source files. Desktop: 730 tests, native build and native Codex configuration/flywheel observation. Draft requires human review and hosted CI.
