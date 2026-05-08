# ADR 0005 — Lessons lifecycle: 60-day TTL with Phase-A-citation pin renewal

- Status: Accepted
- Date: 2026-05-08
- Relates to: ADR 0004 (critic dispatch) — slice 2 of "close the learning loop." Depends on critic scores existing on `EpisodeStore` rows (slice 1).

## Context

`learning/lessons/` ships with a `Lesson` schema, a vector store, an
extractor stub, and an injector stub. None of it is wired into runtime.
PRD line: "each task end to extract a short structured 'lesson' per
specialty (what worked, what didn't) and index it in a vector store, so
that future subtasks can retrieve top-k relevant lessons and inject them
into worker prompts."

Slice 2 wires the pipeline: nightly batch extraction over top-K episodes
by `critic_score` per specialty, runtime injection into worker prompts.
At ~5 specialties × ~3 lessons/specialty/night, the store grows ~5500
rows/year. Some of that grows in value (timeless craft); most becomes
stale because:

1. Lessons describe *current* model behavior. Phase B/C ship adapters
   that change behavior; lessons that predate the adapter become wrong
   in a way that injection actively hurts retrieval-driven outcomes.
2. The cluster's specialties evolve — a lesson for `research-summarize`
   in May might describe a tool routing that the May→July prompt
   evolution fixed away. The lesson is now teaching the obsolete fix.

A pure no-eviction store (option A in the grill) is comfortable on day
one and hostile by month six. A pure TTL store (option B) drops some
genuinely-timeless lessons; re-extraction recovers them but the gap
matters during the days of staleness.

What this ADR pins:

- The eviction rule.
- The signal used to mark a lesson as "earned its keep."
- How that signal is recorded across the lessons-store ↔
  prompt-evolution boundary.

## Decision

### 1. Eviction rule: 60-day TTL by `created_at_ms`

Lessons older than 60 days at the time of the nightly sweep are deleted.
60 days specifically: spans 8–10 Phase A prompt-evolution cycles
(weekly-ish) and 8–10 Phase B promotion cycles, long enough that
lessons survive routine adapter swaps; short enough that a base-model
upgrade does not produce a long tail of misleading injections.

The eviction sweep runs in the same nightly job that runs extraction
(02:00 local). Coupling eviction to the nightly window keeps daytime
retrieval latency bounded and predictable — no per-query expiry filter,
no compaction races during user traffic.

### 2. Pin signal: prompt-evolver citation in a winning A/B prompt

`learning/prompt_evolution/evolver.py` already regenerates per-specialty
system prompts from top-K episodes; an A/B router promotes the new
prompt if win-rate beats threshold. When the evolver emits a *winning*
prompt, it records the `lesson_ids` it consulted while writing that
prompt. Those lessons are pinned.

This is the cheapest available "earned its keep" signal in the
codebase. The alternatives considered:

- **Per-injection lift** (track score deltas with vs. without the
  lesson injected): right answer eventually, but requires a control
  population running the same specialty without lessons. Doubles the
  experimental complexity; defer to a future slice.
- **Training-pipeline contribution** (lesson → episode → SFT/DPO
  dataset → adapter that beat eval-set baseline): the most rigorous
  signal, but predicated on Phase B/C trainers running at cadence,
  which they don't yet. Build the data pipeline when there's data
  flowing through it.

Phase-A citation reuses machinery already in the tree, requires only a
small bookkeeping addition on the evolver side, and has clean
semantics: a lesson the prompt evolver chose to encode into the new
system prompt has been judged useful enough to live longer.

### 3. Pin renewal, not pin permanence

A pinned lesson gets `pinned_until_ms = now + 60 days`. It is
TTL-exempt until that timestamp. If the prompt evolver cites it again
inside the pin window, the pin extends by another 60 days. If no
further citation arrives, the pin lapses and the lesson re-enters the
eviction pool on the next sweep.

This avoids the trap a permanent-pin design walks into: a lesson pinned
in March that the model has since outgrown stays immortal because it
was useful once. Renewal means "stays as long as it keeps proving
itself." The cost is a small bookkeeping field; the win is that the
store does not accumulate a layer of fossilised pins.

### 4. Schema change

```python
@dataclass(frozen=True)
class Lesson:
    specialty: str
    task_id: str
    text: str
    embedding: tuple[float, ...]
    created_at_ms: int                  # new
    pinned_until_ms: int | None = None  # new; None = unpinned
    schema_version: int = CURRENT_LESSON_SCHEMA_VERSION  # bump 1 → 2
```

The schema-version mechanism in `lesson.py` is already in place for
exactly this kind of change — old payloads with `schema_version=1` are
refused with `LessonSchemaError`, not silently mixed.

### 5. Cross-system contract

The lessons store does not call into the evolver. The evolver writes
pin events: when an A/B-winning prompt is promoted, the evolver issues
`LessonStore.pin(lesson_ids, until_ms=now+60d)`. The lessons store has
one inbound API for pinning; it does not introspect prompt-evolution
internals.

This keeps the modules decoupled the right direction: the evolver knows
about lessons (it reads them when generating prompts), but the lessons
store does not know about the evolver. A future second pin source
(e.g. operator manual pin, or per-injection-lift slice 2.5) plugs in
through the same `pin()` API without touching lesson eviction logic.

### 6. Eviction sweep query

```
DELETE FROM lessons
WHERE created_at_ms < (now_ms - 60d)
  AND (pinned_until_ms IS NULL OR pinned_until_ms < now_ms)
```

Run once per nightly batch, after extraction.

## Consequences

- The lessons store has one new write path (`pin(lesson_ids, until_ms)`)
  consumed by `prompt_evolution.evolver`. The evolver becomes a
  required upstream for the lessons-lifecycle invariant — without it
  emitting pin events, every lesson dies at 60 days, including
  legitimately timeless ones. That's an acceptable degradation: the
  store still works, lessons just churn faster.
- A lesson that is genuinely timeless but never makes it into a
  prompt-evolver citation gets evicted at 60 days. Re-extraction from a
  newer episode demonstrating the same behavior recovers it within one
  night. Cost: brief retrieval gap. Acceptable.
- The schema bump from version 1 → 2 means any persisted lessons from
  pre-slice-2 development data are refused on load. Not a real concern
  — there are no real lessons in the store yet.
- Operator visibility: a `pinned` count per specialty is a useful
  Discord/CLI surface eventually (slice 2.5+). Not built in slice 2.
- A future shift to per-injection-lift pinning (option ii from the
  grill) plugs in via the same `pin()` API. No eviction-rule rewrite.

## Out of scope

- Per-injection lift measurement and pinning (slice 2.5+).
- Training-pipeline contribution as a pin signal (waits on slice 4
  shipping promoted adapters at cadence).
- Operator manual pin/unpin CLI.
- Eviction policies other than time-based — e.g. specialty caps, score
  caps. Revisit if the store grows to a size where retrieval latency or
  signal/noise becomes a problem in practice.
- Lesson lineage — recording when a re-extracted lesson is "the same
  lesson" as an evicted one. The vector store handles this implicitly
  (similar text → similar neighborhood); explicit lineage tracking is
  not worth the schema cost.
