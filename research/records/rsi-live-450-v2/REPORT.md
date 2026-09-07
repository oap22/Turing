# Native Codex RSI campaign, issues #450–456

The native Turing terminal launched a real Codex Luna xhigh round that repaired Turing's trajectory-evidence handling. The supervisor accepted the corrected candidate with `passed=true`, `void=false`, score 1 and exit 0. This is a demonstrated software repair, not evidence that recursively edited scaffolds improve general agent quality.

## Results and controls

| Evaluation | Result | Meaning |
|---|---:|---|
| Original 05ab64d through corrected external cases | 2 passed, 6 failed | Six corruption cases fail on the incumbent |
| Preserved seed 1e623e8 | 7 passed, 1 failed | Initial real candidate fixes earlier mutations but misses late verifier writes |
| Corrected real candidate b8d6720 | 358 passed, 4 skipped | Frozen baseline suite plus eight source-selected external cases |
| Supervisor record | 1 accepted round, exit 0 | Real CLI returned successfully and supervisor measured score 1 |
| Integrated branch before final self-edit guard | 386 passed, 4 skipped | Includes new integration regressions and eight external cases |
| Final desktop tests | 730 passed, 42 files | Includes actual shell argv quoting tests |

The corrected attempt took 440.672 seconds (7.34 minutes), estimated 12 minutes. Model settings were `gpt-5.6-luna`, `xhigh`, `workspace-write`; no fallback to Claude. The native shell resolved Codex 0.153.0 through `~/.npm-global/bin/codex`. Subscription dollar usage and provider seed were unavailable. Engine source was clean commit 1cc5b07; candidate source was committed at b8d6720 with an empty source diff. `log_run.py check` passes after the verification section was completed.

## Launcher provenance hardening

The completed v2 run was launched with launcher code from engine source commit `1cc5b07`, before the seed and clean-start guards and before/after source identity fields added in this follow-up. The actual candidate seed `1e623e8` was manually verified at launch, and the preserved source commits and candidate patch remain part of the completed evidence. Existing completed metrics, evaluator files, trajectory, and source artifacts are historical evidence and are not rewritten. Future launcher invocations now refuse a different or dirty candidate source seed before starting Codex and record source identities before and after the run. These guards do not retroactively alter the completed run evidence.

## The failed first attempt

The first attempt ran for 1086 seconds (18.10 minutes). Round 1 exhausted its 600-second engine cap. Round 2 was interrupted after the lead found a verifier configuration mistake: pytest's baseline pyproject `pythonpath` overrode the candidate PYTHONPATH. The verifier failures from that attempt are **invalid evidence about the patch**. The run and partial patch remain preserved under `../rsi-live-450/` and `~/research-results/2026-09-06-turing-rsi-450-live/`; it is not reported as success.

The corrective run has a new slug, frozen manifest and seed. It sets an absolute pytest `pythonpath` override and asserts the imported loop/cheat module paths inside pytest. An independent reviewer deliberately misrouted imports and observed that guard fail. All four test hashes match the manifest. The evaluator was not changed after the corrective launch.

## Changes produced and reviewed

Three Luna xhigh specialists implemented and independently reviewed bounded areas in separate worktrees:

- **#451:** explicit Codex CLI engine, fixed Luna/xhigh settings, bounded subprocess output/time, owning-checkout Python source resolution, no silent engine fallback.
- **#452/#456:** reconstruct pending self-edit judgments across invocation boundaries, retain the original judgment window, refuse unverifiable scaffold authority and refuse resume after a failed rollback.
- **#453:** real RSI candidate snapshots trajectory bytes, detects mutation, quarantines changed evidence and restores trusted history before recording a void round. It covers engine writes and writes arriving during verification. Final review additionally reproduced a same-stat mutation during scaffold editing. The final guard rejects that proposal, restores trusted history, and also restores history when the proposal raises an exception after writing it.
- **#454:** native engine/verifier setup, persisted parameters, exact verifier quoting, safe results-root shell substitution, and visible PTY startup/resize errors with caught cleanup failures.
- **#455:** all-engine-failed invocations and failed rollbacks return nonzero status; normal no-op STOP resumes retain their separate execution semantics.

The launcher evaluates only newly completed rounds, requires the latest round to be accepted, and retains a nonzero process return. Independent executable harness cases reject old-history-only, event-only and pass-then-fail outcomes.

## Native verification

Computer use opened the test macOS bundle, created a dedicated RSI workstation, and launched `/tmp/r450` followed by corrective `/tmp/r450b` in its terminal. The terminal visibly showed the correct engine, source paths, caps and run start; process inspection confirmed the exact model argv. The completed result is established by the supervisor trajectory and process record.

After the run, the app was quit and rebuilt from the isolated worktree. Computer use then created `RSI Codex verified 450`: empty verifier submission showed `verifier command is required`; selecting Codex displayed fixed Luna/xhigh/workspace-write; the saved terminal command contained `--engine codex` and the exact verifier. Reopening retained those settings. The rebuilt flywheel pane selected `loop-rsi-turing-rsi-450-v2` and displayed the accepted round with score 1, exit 0 and void false. Its expanded view reported no `round.json`, so per-round detail navigation remains limited for this CLI record format. The command stays prefilled and requires Enter; no extra model run was started.

The installed `/Applications/turing.app` was not replaced. The inspected test bundle is `desktop/src-tauri/target/release/bundle/macos/turing.app` in the primary repository's build cache. The runner's normal cwd still points to the saved primary checkout; live campaign commands explicitly selected the isolated branch because primary main has an unrelated unresolved merge.

Observed baseline terminal rendering sometimes required a focus/layout refresh, and layout changes can recreate terminal panes. This campaign does not claim to have solved every rendering or scrollback problem. The rebuilt configuration screen and prefilled command were directly visible.

## Evidence and limits

`completed/` contains the raw supervisor record, trajectory, source patch, control and seed failures, integrated test output, web tests and native build log. Private model transcripts are not copied into the repository. Source review used real exported loop/verifier paths, late subprocess writes, same-stat mutation probes and restart cases. Python Ruff, formatting, mypy and bash syntax checks pass; final totals below supersede preliminary totals when present.

The live run used `self-edit-every=0`. Proposal, judgment, rollback and resume are tested deterministically, but a controlled live candidate/incumbent scaffold evaluation (S03), noise-floor measurement and general-quality improvement remain unproven. This is also not a hardened hostile-process security boundary: the engine shares the user's account and the guard detects and restores evidence at defined phase boundaries.

Review tier: **human review mandatory**, because the combined production diff exceeds 400 lines. No merge, release or installation is implied by local green checks. The draft PR description is prepared locally; hosted CI has not started.

## Final review and broader checks

The scaffold-proposal correction was independently attacked with a two-invocation probe: the offending edit is rejected, `self_edits=0`, incumbent best score remains 1, and resume contains only legitimate scores. Ordinary self-edits still work. Proposal exceptions restore trajectory history and preserve quarantine evidence before propagating the original error. The final trajectory regression file has eight passing tests.

The broader Python run produced 3,644 passes, five skips and two socket-bind failures in 137.54 seconds. Both failing tests passed unchanged when rerun with loopback access (2 passed in 1.38 seconds); the sandbox was denying bind before the tested behavior. The dial-guard failure also reproduced on unchanged baseline. The gateway test skipped on the baseline because no generated SPA bundle existed there. Full mypy passes across 277 source files; Ruff lint/format checks cover all 562 Python source/test files.

The first final RSI confirmation produced 388 passes, four skips and one failure in the unchanged 300 ms Claude timeout test: the process was killed before its `child.pid` file existed. The exact test passed on immediate isolated rerun without edits. Its execution code is unchanged from baseline. The first failure and rerun are retained, rather than erased or addressed by weakening the test. A quiet final confirmation is recorded below.

Final confirmation on integrated source 7d409c9: **389 passed, 4 skipped in 55.31 seconds**. No code or test edits were made between the timeout-test failure and this passing confirmation. Human review and hosted CI remain release gates.

## Publication status

Implementation and evidence are committed on `oap22/450-rsi-live-test`. Automatic approval review rejected the combined commit/push because publication of code and experiment records to GitHub lacked explicit user authorization. The local commit was then completed separately. No push or PR was created. `PR-DRAFT.md` is the prepared description; publication requires user approval. The main checkout and installed app remain untouched.

## Authorized publication and fresh review

The owner subsequently explicitly authorized publication, another Luna xhigh adversarial review/fix cycle, and merge through the PR after that review is clean. This supersedes the publication blocker above. Three fresh reviewers cover trajectory/scaffold authority, Codex/CLI boundaries, and native desktop configuration separately. Hosted CI and enforced repository requirements still apply; no administrator bypass is authorized.
