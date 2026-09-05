---
name: multi-agent-workflow
description: How to claim work, branch, and merge in the Turing repo when multiple humans are each running multiple agent sessions in parallel. Load this before starting any non-trivial change — Claude Code, Codex, Cursor, or any other agent working in this repo.
---

Multiple humans each run multiple agent sessions against this repo in parallel.
The rules below keep them from colliding. (Decision record: issue #356.)

## Claiming work

- The unit of work is a GitHub issue; `ready-for-agent` issues are the free queue.
- **The claim is the issue assignee.** Before starting, assign your human:
  `gh issue edit <n> --add-assignee @me`. One agent per issue. Never start on an
  issue assigned to someone else. Unassign to release a claim you abandon.
- Non-trivial drive-by work gets an issue first.

## Branches & worktrees

- Branch naming: `<github-user>/<issue>-<slug>` (e.g. `loducaj/284-coordinator-pt2`).
- One worktree per concurrently-running agent; **one branch = one agent** — two
  sessions never share a branch.
- Close the agent session before removing its worktree; remove worktrees after merge.

## PRs & review tiers

- Everything reaches `main` via PR with green CI. Reference the issue (`Closes #<n>`).
- **Human review is mandatory** when the PR does any of: touches a CODEOWNERS
  hotspot · adds or changes an alembic migration / DB schema · adds or changes a
  dependency · changes a wire contract or gateway API · changes an ADR or
  `CONTEXT.md` · exceeds ~400 changed lines excluding tests and lockfiles.
- Otherwise a **fresh agent session** reviews the PR (findings posted as PR
  comments) and the owning human reads the verdict before merging. (Claude Code
  sessions do this with `/code-review`; use the closest equivalent your agent has.)
  How to run that review so it finds real defects — reproduce before reporting,
  drive real code rather than fixtures, re-review the fixes — is
  `skills/adversarial-review/SKILL.md`.
- Every PR description must self-classify its review tier.

## Sequence rules (hazards git merges cleanly but breaks)

- **Alembic:** migrations form one linear chain. If `main` gained a migration
  while your PR was open, re-point your `down_revision` to the new tip before
  merge. CI enforces single-head (`tests/test_alembic_chain.py`).
- **ADRs:** take the next free number; never reuse one. CI enforces uniqueness
  (`tests/test_adr_numbering.py`; the historical 0001/0010 duplicates are
  frozen grandfathers).
