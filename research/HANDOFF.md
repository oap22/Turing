# HANDOFF — autonomous research agent scaffolding

**Written 2026-08-12 at a session limit. Read this first.**

---

## 1. Stop-and-check before you do anything

A 19-agent workflow was building this scaffolding when the session ended. **It may
have completed, or it may have been killed mid-flight.** ~23,850 lines across 80
files are on disk, uncommitted.

**Do not commit, PR, or merge until you have established which phases actually
ran.** The build phase clearly ran. Whether the *adversarial review, repair,
integration, and verification* phases ran is unknown — and those are the phases
that make this code trustworthy.

```bash
git -C /Users/owenpacetti/Developer/active/Turing status --porcelain
```

The workflow's per-agent record — one `{"type":"result",...}` line per completed
agent — is at:

```
/Users/owenpacetti/.claude/projects/-Users-owenpacetti-Developer-active-Turing/26cf19fb-d635-48c4-b44c-0019e6db4cf2/subagents/workflows/wf_6ea1ef93-d6e/journal.jsonl
```

Read it to see which agent labels completed. Expected labels, in order:
`contracts` → `build:{problems,solver,backends,loop,docs}` →
`refute:{...}` → `repair:{...}` → `integrate` → `verify:invariants`,
`verify:honesty`.

**If `integrate` and the two `verify:` agents did not complete, the work is not
finished** regardless of how much code exists. Resume the workflow rather than
hand-finishing it:

```bash
# Script: .../workflows/scripts/research-agent-scaffolding-wf_6ea1ef93-d6e.js
# Workflow({scriptPath: "<that path>", resumeFromRunId: "wf_6ea1ef93-d6e"})
```
Completed agents replay from cache; only unfinished ones re-run.

---

## 2. What this work is

Turing has been **fundamentally retargeted**. It is no longer "fine-tune four
Jetsons into AI/ML mini-experts" (ADR 0009). It is now **an autonomous research
agent**: given a `(goal, verifier)` pair it works until the frozen verifier
passes, and between projects it rewrites its own skills.

**The source of truth is the agreed brief:**
[`research/briefs/2026-08-12-autonomous-research-agent.md`](briefs/2026-08-12-autonomous-research-agent.md)

It is `status: agreed`, produced by a full `research-interview` session, and it
records every decision *with the reasoning and the rejected alternatives*. **Read
it before changing any design choice** — several decisions look arbitrary until
you see what they were protecting against.

## 3. Current state

| Item | Value |
|---|---|
| Branch | `oap22/380-research-agent-scaffolding` (created from `main`, clean at branch time) |
| Issue | [#380](https://github.com/oap22/Turing/issues/380), assigned to `oap22` |
| Commits | **None yet.** Everything is uncommitted working-tree state. |
| Code | `src/turing/research/` — contracts, problems, solver, backends, loop |
| Tests | `tests/test_research/` — ~40 test modules |
| Docs | `docs/adr/0011-autonomous-research-agent-retarget.md`, `docs/research-agent.md` |
| Scripts | `scripts/setup-research-sandbox.sh`, `scripts/bootstrap-turing-skills.sh` |
| Research dir | `research/` — JOURNAL, OPEN-QUESTIONS, DEAD-ENDS (intentionally empty), results/ |
| Stray file | `tests/test_research_scaffolding.py` — **unexpected**; not in any agent's assigned paths. Inspect it; it may be a stray or a misplaced integration test. |

## 4. What the user asked for, verbatim

> "build the scaffolding with a workflow using adversarial review. When done open
> a pr and merge"

So the remaining work is: **finish/verify → commit → PR → merge.** The user has
explicitly authorised the merge.

**But flag this in the PR body, honestly:** the repo's own `CLAUDE.md` makes human
review *mandatory* for a PR that adds an ADR or exceeds ~400 changed lines
excluding tests. This does both, by a wide margin. The user authorised merging
anyway; self-classify the tier accurately rather than quietly marking it
agent-reviewable. The conventions require the PR to state its tier and whether
GitNexus impact analysis ran (it did not — this is additive new-file work, which
`CLAUDE.md` exempts).

## 5. Exact next steps

1. **Establish workflow completion** (§1). Resume if incomplete.
2. **Run the checks yourself — do not trust agent reports.** Agents have claimed
   green suites before without running them.
   ```bash
   cd /Users/owenpacetti/Developer/active/Turing
   .venv/bin/pytest tests/ -q
   .venv/bin/ruff check src/ tests/
   .venv/bin/mypy src/
   .venv/bin/python -c "import turing.research"
   ```
   Note: some failures on this branch may be **pre-existing**. Establish that by
   checking whether they involve `tests/test_research/`. Fix only ours.
3. **Verify the six invariants hold** (§6). These are the whole point.
4. **Resolve the stray `tests/test_research_scaffolding.py`.**
5. **Commit, PR against `main` referencing `Closes #380`, merge.**

## 6. The six invariants — verify these before merging

Any violation is a critical defect, outranking every local bug:

1. **The verifier is frozen.** No path — reflection, `dataclasses.replace`, mutable
   default, shared reference, subclass override, config injection — by which
   solver-side code can mutate, relax, or regenerate a verifier or its thresholds.
   *This is the single load-bearing guarantee of the system.*
2. **The agent cannot quit.** No path to terminating a project without an operator
   `ABANDON` decision. Hopeless projects **escalate**, they do not abort.
3. **No advice channel.** Escalation decisions are strictly
   `CONTINUE | ABANDON | EXTEND_CAP`. No free text. Free-form guidance would make
   the operator the improvement mechanism and confound the next round's delta.
4. **No cross-type averaging.** Speedup and Kaggle scores are reported separately,
   never blended into one headline number — a blend hides a round that helped one
   type and hurt the other.
5. **No held-out leakage** into anything a future loop-2 self-edit step reads.
6. **The cap cannot be exceeded**, and token/step accounting does not under-count.
   The cap replaced the retired dollar `BudgetGate` as the runaway-loop brake.

## 7. Scope boundaries — do not let these creep

**In scope (loop 1):** within-project autonomous solving with a *frozen* scaffold.

**Explicitly NOT in scope (loop 2):** self-editing, the cheat detector, rollback,
harness-isolation enforcement. Seams for them exist and are marked; leave them
unimplemented. If you find any of it built, that is scope creep and should be
reported.

**Why the ordering matters:** loop 1 must produce a real round-0 solve rate and a
noise floor first. Without a baseline there is nothing for self-modification to
improve on and no signal telling it what to change. Also, the four riskiest items
are all loop-2 items — nothing is self-editing during loop 1, so there is no
harness to game and nothing to roll back.

## 8. Things that will bite you

- **The `.venv` exists.** Use `.venv/bin/python`, `.venv/bin/pytest`, etc. Bare
  `pytest` on PATH does not see the project.
- **ADR numbering:** 0011 is correct and next-free. 0001 and 0010 have
  grandfathered duplicates. `tests/test_adr_numbering.py` enforces uniqueness;
  `tests/test_alembic_chain.py` enforces a single migration head.
- **Do not modify existing `src/turing/` modules.** The coordinator, NATS,
  mesh, worker, gateway, webui and TUI code is **deliberately dormant, not
  deleted** — greenfield-with-selective-reuse was an explicit decision. Four
  things survive on merit and are *reused, not copied*: the safety layer, the
  episode store, the promotion/canary/rollback machinery, and
  `coordinator/alerts/ntfy_client.py` (escalation delivery).
- **`muse-glimmer` / local models are NOT in the loop.** Everything runs on the
  user's Claude subscription. A `LocalBackend` stub exists purely as a seam. The
  ~80–90 tok/s figure for Nemotron 3.5 Lightning is an **inference from a
  same-family benchmark, never measured on this machine** — it must stay labelled
  that way everywhere it appears.
- **No LLM inference on ROSIE, ever** — the user was explicit. The H100s are held
  in reserve. Under the current plan there is no H100 requirement at all.
- **The user is not deep on ML-experiment methodology** and asked for plain
  language twice during the interview. Explain jargon; don't assume terms like
  "corpus", "held-out", or "Goodhart" land.

## 9. The corpus, for when loop 1 runs

**11 problems: 6 speedup + 5 Kaggle**, scored separately, split ~7 practice /
~4 held-out. **Locked before the noise floor and unchangeable after** — changing
the eval set restarts the trajectory.

The **6 speedup problems are fully sourced and profiled** (measured on this M4
Pro, ≥2 runs each) — full table, headroom figures, correctness gates, and six
verified *gate traps* are in the brief under "Headroom profiling". The gate traps
matter: e.g. on one candidate **the correct optimization changes the answer**
because hoisting a computation removes its consumption of the torch RNG stream,
so a gate pinning the output would fail the intended fix.

**The 5 Kaggle competitions are NOT yet selected.** That is the remaining
sourcing work. The selection criterion is **mechanical, pre-registered, and about
feasibility only** — "completes end-to-end on this Mac in under N hours with the
baseline solver." **Never expected score, never 'looks promising.'** If problems
enter because the agent does well on them, every downstream number is invalid.

## 10. Open questions still live

Tracked in the brief's Open Questions table and seeded into
`research/OPEN-QUESTIONS.md`. The ones that block progress:

- **Q12** — contents of the frozen daily-task fixture set (the Goodhart detector).
  Blocks round 0. Must be recorded fixtures, not live state, or the measure drifts
  on its own.
- **Q11** — how harness read-only-ness is *enforced* (process, container, mount).
  Blocks loop 2.
- **Q15** — the frozen error taxonomy for round summaries. Blocks the first
  self-edit.
- **Kaggle selection** — see §9.

## 11. Related loose end

A **real bug was found in Maestro** while profiling, unrelated to this branch:
`packages/core/src/retry.ts` imports three `DEFAULT_TRANSIENT_RETRY_*` constants
that `@maestro/shared` never exports, so the retry loop is unbounded and
busy-spins. `retry.test.ts` takes **507 seconds** — 96% of Maestro's entire suite
runtime — with 3 tests failing. A background task chip was created for it. It is
also speedup problem #4 in this corpus, so **fixing it in Maestro would consume
the benchmark problem** — decide deliberately which you want.
