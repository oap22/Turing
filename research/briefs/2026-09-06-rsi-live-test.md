# Turing RSI live application test (#450)

Status: agreed engineering campaign; live run preparation.

User authorization: run Turing with computer use, thoroughly test its RSI workflow, and use GPT-5.6 Luna at xhigh to implement app improvements; user additionally confirmed Codex support.

## Outcome and acceptance

1. Exercise the native workstation creation and launch path.
2. Add explicit Codex/Luna/xhigh execution with bounded process time and output, retaining Claude compatibility.
3. Reproduce and fix practical execution/reporting defects with independent probes.
4. Run a bounded real-model iteration against a clean Turing source snapshot with a supervisor-owned frozen verifier. Validate the resulting source against the baseline and independent tests; distinguish a useful code patch from causal evidence of improved self-edit policy.
5. Preserve source, raw logs, model settings, candidate diff, failures and review evidence. No automatic promotion or merge.

## Isolation and ownership

Main at 05ab64d has an existing unresolved merge and unrelated dirty edits. Lead works in .worktrees/450-rsi-live-test on oap22/450-rsi-live-test. Agents each have separate worktrees. #451 owns Codex engine + CLI/bridge. Other implementation issues follow reproduced defects. Shared dependency executables may be used with explicit candidate PYTHONPATH; source provenance must not come from the editable main install.

## Test design and limits

Hypothesis: the RSI workstation can launch the requested model, accept only externally verified source improvements to Turing, and expose truthful terminal/round outcomes.
Measurement: successful native launch, verifier outcome, exact candidate patch, externally reproduced regression cases and preservation of existing checks.
Control: immutable main snapshot through the identical verifier. Each implementation regression is exercised against baseline and candidate.
Falsifier: model substitution, a failed/no-op candidate reported as improvement, editable verifier authority, native inability to launch/observe the run, or existing behavior regression.
Primary endpoint for engineering checks: correctly handled independent cases. This campaign does not estimate general LLM quality or the causal effect of SCAFFOLD self-editing. No stochastic performance/saturation claim is planned; a three-seed noise floor would be required before such a claim.
Budget: at most three concurrent Luna xhigh specialists; initial live attempt capped at two rounds, 600 seconds per engine round, one self-edit at most (only if justified after successful round execution), verifier cap 120 seconds. Stop on tampering, unclear authority, repeated engine failure, no eligible source improvement, or exhausted rounds. No API key purchases, GPU jobs or infrastructure. Codex uses existing authenticated subscription; marginal dollar cost is unavailable, not recorded as zero. Expected overall interactive engineering/test time 30–60 minutes, live attempt <=22 minutes plus optional bounded self-edit. Any failed attempt counts toward evidence and time.

## Initial observations

- Installed /Applications/turing.app opened through CUA. Default workstation metrics/images were visible.
- Created dedicated 'Turing RSI 450' workstation using Home -> n -> RSI -> name -> problem.
- Default RSI command uses Claude legacy bash path, ten rounds, no verifier, no Turing source seeding.
- Both new native terminals appear blank despite live /bin/zsh children; under diagnosis. No RSI run has started.
- CLI supports only Claude/fake on baseline; #451 implements explicit Codex.

## Checkpoint

Work in progress. Do not cite this document as a completed experiment. Native launch, live source improvement, verification and review remain pending.

## Corrective attempt, separately identified

Original live attempt launched through the native terminal with Codex Luna xhigh. Round 1 exhausted its 600 second engine cap; round 2 was interrupted when independent source inspection found the frozen verifier imported baseline code because pytest pythonpath overrode the environment. Its verifier failures are invalid evidence about the candidate. Preserve the original record at ~/research-results/2026-09-06-turing-rsi-450-live (1086 seconds, exit 130); do not edit its verifier or recast it as success.

Corrective v2 uses a new slug, manifest, and committed seed 1e623e8 preserving the interrupted candidate. Its eight external cases assert actual module paths inside pytest. The seed passes seven and fails the late verifier write restoration case. The bounded correction is one 600 second Codex round plus one verifier capped at 120 seconds, approximately 12 additional minutes, using the existing subscription. Marginal dollar usage is unavailable. Target only the demonstrated post-verifier corruption defect; no causal self-edit claim. The engine source is committed separately before launch.
