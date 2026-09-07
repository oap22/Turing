# RSI Codex contract fix checkpoint

- Branch: `oap22/457-codex-contracts`
- Worktree: `/Users/owenpacetti/Developer/active/Turing/.worktrees/457-codex-contracts`
- HEAD: `47287ffb57693a9ad7201a06ad970ff016f160c0`
- Modified files: none. The attempted verifier patch did not apply.

## Confirmed findings

1. Resume pin-set comparison is subset-only in `src/turing/research/rsi/cli.py:423` and `src/turing/research/rsi/verifier.py:163`. A lock with `verify.sh` and `extra.txt` resumes successfully when the operator passes only `--verifier-file verify.sh`, despite the documented exact-set refusal contract. Existing lock contents remain active, so this is a provenance/contract mismatch.
2. `research/records/rsi-live-450-v2/run_live.py` does not validate the declared `launch-config.json` seed commit or a clean candidate source tree before launch. A temporary fake-root probe started from an arbitrary candidate commit, emitted one accepted round, and the unmodified launcher returned 0 with `latest_round_accepted=True`.

## Evidence already run

- Engine/CLI/failed-invocation suite: `80 passed, 3 skipped`.
- Bash bridge, Codex engine, and invocation-status tests: `18 passed`.
- Ruff and mypy passed for reviewed engine/CLI/init files.
- `bash -n scripts/rsi-loop.sh` passed.
- Installed Codex accepted the fixed `exec -m gpt-5.6-luna -c 'model_reasoning_effort="xhigh"' --sandbox workspace-write --color never` argv shape.
- Source-resolution, process timeout/cleanup, no-fallback, all-engine-failed, verifier-failure, and no-op STOP acceptance probes were reproduced.

## Remaining implementation

- Add a shared canonical effective verifier-file-set helper, including the historical auto-first-token pin, and use it in CLI dry-run plus real resume loading. Keep `spec=None` resume lock-authoritative.
- Harden `run_live.py` with frozen seed resolution, clean `src`/`tests` start validation, and before/after source-tree identity fields. Preserve existing historical completed metrics/evidence.
- Add focused false-seed/dirty-seed and omitted-pin regression probes/tests; do not run a model campaign.
- Document that the historical v2 run used launcher commit `1cc5b07`; future launcher hardening does not retroactively alter that evidence. Note that the actual v2 seed was manually verified at launch and preserved source commits/patch artifacts.
- Next exact action: resume in this worktree, patch only the assigned files/tests/docs, run focused local tests, commit the coherent slice, and report the commit hash for independent final review.
