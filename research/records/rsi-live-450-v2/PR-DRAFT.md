Turing RSI can now run Codex with explicit GPT-5.6 Luna/xhigh settings, and the desktop requires a verifier when creating an RSI workstation. A real Codex round launched through the native Turing terminal repaired trajectory corruption in Turing itself; the supervisor accepted the candidate with score 1, exit 0 and no void flag.

This change also preserves pending scaffold judgments across restarts, settles completed windows before another round, refuses unverifiable or failed rollback authority, reports terminal execution failures correctly, and restores trusted trajectory history around engine/verifier/self-edit phases, including cancellation. Native launch quoting and PTY error reporting are hardened; clean terminal exits no longer trigger stale resize errors. Verifier resumes require the same effective pin set, and the recorded campaign launcher verifies its starting commit and clean source before running.

Validation:

- Real corrective run: 7.34 minutes, one accepted round; frozen evaluator 358 passed, 4 skipped.
- Original code fails six of eight external corruption cases. The initial candidate fails the remaining late verifier-write case; the corrected candidate passes it.
- Final integrated RSI/evaluator suite: **400 passed, 4 skipped**. An unchanged 300 ms timeout test failed once on earlier source before a clean confirmation; that evidence is preserved.
- Broader Python run: 3,644 passed; two sandbox socket-bind failures both passed unchanged with loopback access. Full mypy: 277 source files.
- Desktop: 733 tests in 43 files, web build and macOS app bundle build pass. Computer use verified required verifier, Codex selection, persisted command, accepted round in the flywheel pane, and the exited-PTY regression.
- Ruff and formatting pass across 564 Python source/test files plus the campaign launcher, as do full mypy (277 source files plus launcher and its test), and shell syntax. Three fresh Luna xhigh specialists reviewed distinct boundaries; six reproduced findings were fixed and independently re-reviewed. Details and remaining limits are in `research/records/rsi-review-457/FINAL-REVIEW.md`.

The first live attempt is preserved as invalid verifier evidence: pytest imported baseline source despite candidate PYTHONPATH. The corrective evaluator has an absolute pytest override and an in-pytest source assertion. The record and remaining limits are in `research/records/rsi-live-450-v2/REPORT.md`.

This establishes a specific code repair, not causal scaffold self-improvement. Live scaffold editing was disabled; proposal/judgment/rollback/resume correctness is tested deterministically. The installed app and unrelated unresolved merge in the primary checkout were left intact.

Review tier: **human review mandatory** (production diff exceeds 400 lines). The human owner explicitly authorized publication and normal PR merge after a clean Luna xhigh review/fix cycle and green CI. Agent review is not a GitHub human approval. Enforced repository requirements will be honored without an administrator bypass.

Closes #450
Closes #451
Closes #452
Closes #453
Closes #454
Closes #455
Closes #456
