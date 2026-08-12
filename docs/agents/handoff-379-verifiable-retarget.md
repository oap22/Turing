# Handoff — #379 Verifiable-Results Retarget

**Branch:** `claude/turing-refactor-abilities-fxt7vd` · **Issue:** [#379](https://github.com/oap22/Turing/issues/379) · **Started:** 2026-08-12

Living document. Read this first if you are picking the work up mid-flight.

## Where the direction comes from

Owen's vault, `02-Projects/Turing.md` § "Next Phase — Verifiable-Results Retarget" (committed
2026-08-12 as `cea82e6` in `oap22/owens-awesome-vault`). That section is the spec of record for
*intent*; this branch is the spec of record for *implementation*.

## The four decisions that scope this work

Answered by Owen at session start. They are settled — do not relitigate them, and do not quietly
widen or narrow them.

| # | Question | Answer |
|---|----------|--------|
| 1 | What is being retargeted? | The flywheel's training objective **and** the worker tool layer (a code-execution tool is in scope, because the code-verified task family needs workers to run code). |
| 2 | Which verifiable task families? | Exactly two: **programmatic math/numeric generators with exact checkers**, and **code tasks scored by hidden unit tests**. Mined arXiv items and an ML/AI-quantitative family were explicitly excluded. |
| 3 | How far does this session go? | New ADR + **full implementation with real pytest coverage**. The ROSIE/SLURM sweep is **deferred** to a follow-up issue; the design must leave a clean seam for it. |
| 4 | What happens to the prose evals? | **Retire the axis entirely.** |

### The one flag raised, and its resolution

Retiring the prose eval axis removes the only measure of risk #1 in Owen's own note — "verifiable
≠ what you care about". Optimizing for checkable answers improves checkable answers; nothing left
in the system detects if general research ability fails to follow. Owen chose full retirement with
that tradeoff stated in the option text. It is therefore deliberate, and the ADR records it as a
**reversible** decision rather than letting it disappear silently.

## The disambiguation that matters most

Two different things share the name `research-summarize`. Confusing them would gut the coordinator.

- **`Specialty.RESEARCH_SUMMARIZE = "research-summarize"`** in `src/turing/coordinator/planner/schema.py`
  is a worker **specialty / job type**. It appears in ~319 places across the planner, registry,
  canary gate, lessons, and critic — mostly as test fixture values. Turing still writes research
  notes to the vault; that is the **product**. **This stays.**
- **`src/turing/evals/research_summarize/`** + `evals/research-summarize/cases/*.jsonl` +
  `scripts/eval_research_summarize.py` + the prose-specific scorers in
  `src/turing/learning/eval_set/` (`case.py`, `scorers.py`, `judge.py`, `harness.py`) are the prose
  **eval axis** — the **training objective**. **This goes.**

"Retire it" means the axis, not the specialty.

## What must survive the change

`src/turing/learning/eval_set/collapse_gate.py` is the constraint layer and is **not** part of the
retirement. Its diversity floor and tail-coverage floor must keep working on the verifiable path.
Note its `EvalRun` contract: `scores` and `outputs` are aligned tuples, both required — so a
verifiable run has to produce generated text as well as numeric scores, or the diversity metric
silently loses its input.

The distinction driving the whole change: `collapse_gate` is a **gate** (ship / don't ship).
What is being added is an **objective** (how much better, at what cost, converging or not).

## Environment notes for this container

- No pre-built venv. `python3.11 -m venv .venv && .venv/bin/pip install hatchling && .venv/bin/pip install -e ".[dev]"`.
  Call `.venv/bin/pytest`, `.venv/bin/mypy`, `.venv/bin/ruff` directly — the ones on `PATH` do not see the project.
- Tests that create git commits need `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null`
  (the container's global git config enables commit signing). With that set from the start, the
  ~40 signing failures `CLAUDE.md` warns about do **not** occur. **Scope it to the `pytest` call.**
  Applying it to your own `git commit` strips the SSH signing config and yields Unverified commits;
  this branch hit that and was fixed by `git rebase --exec "git commit --amend --no-edit --reset-author"`.
- `tests/integration/` and `tests/test_integration/` **hang forever** — no docker daemon, and
  `testcontainers` blocks instead of failing fast. Exclude both. Verified baseline on this branch's
  parent: **1728 passed, 2 skipped in 75.81s**. Anything red beyond that is yours.
- GitNexus is **not** available in this session — no MCP tools, no local index. The `MUST run
  impact analysis` rules in `CLAUDE.md` are scoped to "when the local index is available", so they
  do not apply; the PR states this explicitly rather than claiming the analysis ran.
- ROSIE is unreachable from here, and Linear OWE-12/13/14 (PAT, long-running job, multi-GPU/SLURM)
  are still open. Any SLURM work would ship dry-runnable and unit-tested only — which is why it was
  deferred instead.

## Deferred, deliberately

- **The ROSIE SLURM array sweep.** One array task = one full flywheel run to *R* rounds at a fixed
  seed and config; the matrix sweeps question-selection policy × accumulate-vs-replace ×
  retrain-from-base-vs-stack × LoRA rank. The Jetson fleet stays the *deployment* target; ROSIE
  becomes the *science* platform. This also routes around the June multi-node networking blocker,
  since the sweep does not need the mesh to work.

## Status

<!-- status:start -->
In progress — survey and design phase.
<!-- status:end -->
