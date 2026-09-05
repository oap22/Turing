---
name: adversarial-review
description: How to review a change in the Turing repo so the review actually finds defects — reproduce before reporting, drive real code, re-review the fixes. Load this when reviewing a PR (the `multi-agent-workflow` skill's fresh-agent review tier), when auditing a subsystem, or before declaring work ready to merge.
---

The companion to `skills/multi-agent-workflow/SKILL.md` § PRs & review tiers,
which says *who* reviews. This is *how*, so the review finds real defects
instead of confirming a green suite.

Derived from the RES-12 reporting-contract review (PRs #401–#404), where four
rounds found 16 defects — every one in code that was well-argued,
well-documented, and passing a green test suite.

## The first rule

**Reproduce before you report; re-verify after the fix.** A finding you have
not executed is a hypothesis. Write a probe script that fails on the current
code, keep it, and re-run it against the fix — separately from whatever tests
the fix ships with. A fix that only satisfies its own tests is not verified.

## Where defects actually were

- **At boundaries, not in logic.** Every round found the previous fix was
  *correct locally and incomplete at its edge*: a round refused to invent a
  gain from its own lost attempt but not from its parent's; reconciliation
  checked the log's fields but not its identity; a guard covered the round
  loop's re-drive shape but not the noise floor's. Review seams between
  components before reviewing any component.
- **In checks that cannot fail.** Twice, a check compared two things derived
  from the same source, so agreement was definitional. Ask of every assertion:
  *are these two values independently derived?* If not, the check is decoration.
- **In prose that outran the code.** Docstrings promised guards that were never
  implemented. Verify a claim by running it, never by reading it.
- **In "conceded" edge cases.** A limit dismissed as exotic turned out to be the
  shipped recovery workflow. For every conceded limitation, ask whether a
  documented procedure reproduces it.
- **At the consumer.** A contract nobody has consumed is unverified. Boot the
  UI (or call the API) against real emitted artifacts — a producer can be
  perfectly honest while the reader draws fabricated curves.

## Method

- **Drive real code, never fixtures.** Hand-built fixtures hid six false alarms
  in #401 and could not have caught most of what the four rounds found. Drive
  the real runner, the real CLI via subprocess, the real frontend in a browser.
- **Try shapes nobody specified** — killed mid-run, re-driven, crashed before
  the first write vs after it, nested identifiers, a corpus that loses one item.
  Most findings came from a shape absent from the spec.
- **Mutation-test the regression tests.** Revert the fix in each way it could
  plausibly be written wrong and confirm the corresponding test fails. A test
  that passes against the broken code guards nothing.
- **Re-review the fixes.** A fix is a new change with new defects — rounds 2, 3
  and 4 each found real problems in the previous round's work, twice more
  serious than the defect being fixed. Stop when a round comes back clean, not
  when the first round is done.
- **Separate "works" from "works under my harness."** If you stub, list what the
  stub could not reproduce and say so in the report. Make the stub strictly
  no-more-permissive than what it replaces; a permissive stub manufactures a
  false green.

## Fixing what you find

- **Fix at the producer, not the check.** Weakening a check to stop an alarm
  trains the operator to ignore alarms. If a verifier fails honest data, the
  emitter is wrong.
- **Refuse rather than caveat.** A number that exists gets read; a warning
  beside it does not. When an input is missing or an invariant is broken, refuse
  to emit the derived value.
- **Refuse, don't fall back, when the fallback answers a different question.** A
  fallback that says "fine" in exactly the broken case is worse than "unknown".
- **Contain, don't loosen.** When one bad item aborts a batch, add per-item
  containment; keep the strict validation.
- **If a legitimate test fails under your new rule, the rule is wrong** — fix
  the rule, never weaken it to make a suite green. Replace a test's *trigger*
  rather than its assertions.
- **Preserve deliberate decisions.** Ask what the author asked you not to
  "fix" — an asymmetry or a weak check is often load-bearing, and #401's were
  all correct. Verify they survived the fixes.

## Delegating review

- **Give each agent an explicit file lane.** Concurrent edits to one file
  collide; disjoint ownership parallelises safely.
- **Name what is deliberate and must not be harmonised**, or an agent will tidy
  away a decision.
- **Tell reviewers to argue back.** Two sub-agents corrected premises in their
  own instructions and were right both times. Ask for reasons, not compliance.
- **Say that manufacturing a finding to look thorough is itself a failure**, so
  a clean verdict stays trustworthy.
