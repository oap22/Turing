# PR #457 final adversarial review

The owner resumed PR #457 after the internet-related pause and explicitly authorized Luna xhigh fixes, independent review, publication, and normal PR merge after clean review and CI. Work stayed in isolated worktrees. The primary checkout's unrelated unresolved merge and the installed app were preserved.

Three fresh GPT-5.6 Luna xhigh specialists reviewed distinct authority, Codex/CLI, and desktop boundaries. Six concrete findings survived reproduction, including one caught while independently testing a fix:

| Starting state and action | Wrong result | Correction and evidence |
|---|---|---|
| Exit a scratch PTY, switch workspaces, return | False `terminal resize failed: no such pty` | Clear exited PTY identity, including exit-before-spawn-resolution. Mounted regressions fail before/pass after; native replay passed. |
| Propose scaffold edit at round 3, then fail engine rounds 4–6 and resume | Judgment delayed until after unintended round 7 | Settle a complete window before terminal failure and before another engine round. Independent saved probe observes rollback at round 6. |
| Forge trajectory during self-edit, then raise `CancelledError` | Forged round 777 and score 999 survive into trusted resume history | Restore trusted bytes, quarantine mutation and clean ordinary partial edits before propagating cancellation. Independent restart probe contains only legitimate rounds. |
| Resume a locked verifier while omitting one explicitly pinned file | Different requested pin set silently accepted | Compare complete canonical effective pin sets, including the command's auto-pinned file. A resume without overrides still uses the stored lock. |
| Launch the recorded campaign from a different seed or dirty source tree | Invalid starting state can be reported as accepted repair | Validate configured seed and clean `src/tests` before invoking the engine; retain before/after source identities. Historical artifacts remain unchanged. |
| Add an ignored file under `src/`, then mutate it during a stubbed launch | Preflight allows it and before/after identities miss the mutation | Follow-up `dba9874`, integrated as `1317ee8`, compares actual source/test files with committed blobs and includes ignored files in physical provenance. Real-entry-point negative and clean positive regressions pass. |

## Independent review

The desktop reviewer independently inspected authority tip `27e50ae` (including `f950809`) against `47287ff`, replayed both saved failure probes, and found no actionable issues. Focused real-source checks passed: 11 tests plus a separate rollback-failure test. Zero-round/STOP resumes and persistent rollback failures retain their existing semantics.

The authority reviewer checked Codex boundary commit `d0bccca`, including the launcher's real `main()` path. Canonical verifier pin sets and lock-authoritative resumes were clean (72 focused passes, one skip; full RSI 386 passes, four skips). The real-entry-point probe found the ignored-source gap above.

The same independent reviewer then verified follow-up `dba9874` and returned a clean verdict. The [retained real-main probe](final/probe_boundary_launcher.py.txt) reached one stubbed launch and missed the ignored-file mutation before the fix; afterward it refused the unexpected file, reached zero launch calls, and created no metrics. The probe is preserved verbatim; its `launcher_path` identifies the review worktree and must be pointed at the intended source when replaying elsewhere. A pristine seed reached exactly one stubbed launch. An independent seed with an executable, symlink, and newline/tab filename also validated. Focused checks passed **75 tests with one skip**, and the worker-tree RSI suite passed **389 tests with four skips**. No further actionable findings remained in this bounded review round. The lead separately reran the complete integrated tests below.

## Native evidence

See [NATIVE-RECHECK.md](NATIVE-RECHECK.md) for the CUA replay and process evidence. Full frontend suite on the PTY fix passed 733 tests in 43 files, and the macOS bundle rebuilt successfully. The native check verifies the false resize diagnostic is gone; the older canvas/scrollback limitation remains outside this fix.

## Lead verification on integrated source

- Combined RSI and frozen external evaluator: **397 passed, 4 skipped in 64.39 seconds**, with both `PYTHONPATH` and pytest's `pythonpath` explicitly pointing at this worktree's source. Raw output: [final/pytest.txt](final/pytest.txt).
- After the ignored-source correction, final source `1317ee8` passed **400 tests with four skips in 64.65 seconds**, using the same explicit source selection. Raw output: [final/pytest-ignored-source-fix.txt](final/pytest-ignored-source-fix.txt). This supersedes the earlier `95d3b7a` count above.
- Ruff lint and formatting: passed across 564 source/test files, plus the campaign launcher.
- Full mypy: passed across 277 source files plus the launcher and its regression file (279 checked paths, existing unused `torch.*` override note only).
- Shell syntax: `scripts/rsi-loop.sh` and the frozen v2 verifier pass; `git diff --check` passes.
- All four frozen evaluator test files match their manifest. Manifest SHA-256 remains `6fc2b5ca1880fa413e9ef6872a3ed6d18a1d73d9bcb743c05e468c0477bd4c20`.
- Remote main refreshed to `05ab64d`, already an ancestor of the integrated source. Hosted CI on the final published commit remains a merge prerequisite; the earlier green run on `47287ff` does not cover these fixes.

## Evidence boundaries

The real Codex Luna xhigh campaign remains one accepted code-repair round (440.672 seconds), with 358 frozen evaluation passes and four skips. Live scaffold editing was disabled. Deterministic recursive lifecycle checks do not establish causal improvement in general agent quality. No live model campaign was repeated merely to resume PR review.

The first invalid-source campaign, the unchanged timeout-test flake, and sandbox-specific socket failures remain documented in the campaign report. Frozen evaluator test files and historical run metrics are unchanged by this review.

Review tier remains **human review mandatory** because the production diff exceeds 400 lines. The owner explicitly authorized conditional merge in this conversation. Agent review is not a GitHub human approval; enforced branch requirements will be honored without an administrator bypass.
