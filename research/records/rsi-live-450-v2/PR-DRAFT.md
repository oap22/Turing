Turing RSI can now run Codex with explicit GPT-5.6 Luna/xhigh settings, and the desktop requires a verifier when creating an RSI workstation. A real Codex round launched through the native Turing terminal repaired trajectory corruption in Turing itself; the supervisor accepted the candidate with score 1, exit 0 and no void flag.

This change also prevents pending scaffold judgments from being lost across restarts, refuses unverifiable or failed rollback authority, reports terminal execution failures correctly, preserves trusted trajectory history around engine/verifier/self-edit phases, and hardens native launch quoting and PTY error reporting.

Validation:

- Real corrective run: 7.34 minutes, one accepted round; frozen evaluator 358 passed, 4 skipped.
- Original code fails six of eight external corruption cases. The initial candidate fails the remaining late verifier-write case; the corrected candidate passes it.
- Integrated RSI/evaluator suite: 389 passed, 4 skipped (an unchanged 300 ms timeout test failed once before a clean confirmation; preserved in the report).
- Broader Python run: 3,644 passed; two sandbox socket-bind failures both passed unchanged with loopback access. Full mypy: 277 source files.
- Desktop: 730 tests, web build and macOS app bundle build pass. Computer use verified required verifier, Codex selection, persisted command, and accepted round in the flywheel pane.
- Ruff, Python formatting, mypy and shell syntax checks pass. Three Luna xhigh specialists implemented and independently reviewed the changes; reproduced findings were fixed and re-reviewed.

The first live attempt is preserved as invalid verifier evidence: pytest imported baseline source despite candidate PYTHONPATH. The corrective evaluator has an absolute pytest override and an in-pytest source assertion. The record and remaining limits are in `research/records/rsi-live-450-v2/REPORT.md`.

This establishes a specific code repair, not causal scaffold self-improvement. Live scaffold editing was disabled; proposal/judgment/rollback/resume correctness is tested deterministically. The installed app and unrelated unresolved merge in the primary checkout were left intact.

Review tier: **human review mandatory** (production diff exceeds 400 lines). The human owner explicitly authorized publication and merge after a fresh Luna xhigh review/fix cycle is clean. That fresh review and hosted CI are in progress; enforced repository requirements will be honored.

Closes #450
Closes #451
Closes #452
Closes #453
Closes #454
Closes #455
Closes #456
