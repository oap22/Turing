# ADR 0006 — Reward attribution model: event-table, additive, with critic fallback and reconcile-cancel

- Status: Accepted
- Date: 2026-05-08
- Relates to: ADR 0004 (critic dispatch — produces the score the fallback consumes); ADR 0005 (lessons lifecycle — same nightly window houses the fallback sweep). Slice 3 of "close the learning loop."

## Context

The PRD specifies a layered reward signal for each closed episode:

- **Subtask-thread thumb** on a per-subtask Discord thread → that
  subtask's episode (highest weight).
- **Main-message thumb** on the live-DAG message → the synthesis
  episode (full weight) **plus** fractional credit (±0.3) to
  non-synthesis subtasks whose workspace keys synthesis read.
- **No feedback in 24 h** → critic score becomes the reward.

In the current tree, none of this exists: `EpisodeStore` has no
user-score column, no rewards table, no Discord reaction listener, no
mapping from Discord surfaces back to episodes. Slice 3 wires it.

The PRD's wording is loose in places that turn out to be load-bearing:

- "Highest weight" for subtask thumbs — does that mean *strict
  precedence* (only the subtask thumb counts when present) or
  *attribution coefficient* (1.0 vs. 0.3, additive)?
- What does "consumed upstream" mean operationally — the DAG
  `depends_on` set, or the keys synthesis actually read?
- What happens when the operator un-thumbs after the bot was offline
  during the un-thumb event?

This ADR pins the answers and fixes the storage shape that supports
all three.

## Decision

### 1. Storage: separate event table, additive

Rewards live in a new `episode_rewards` table:

```
episode_id          INTEGER NOT NULL
source              TEXT NOT NULL    -- enum: subtask_thumb,
                                     -- synthesis_thumb_fractional,
                                     -- critic_fallback
value               REAL NOT NULL    -- signed; range varies by source
recorded_at_ms      INTEGER NOT NULL
discord_user_id     TEXT NULL        -- NULL for critic_fallback
discord_message_id  TEXT NULL        -- NULL for critic_fallback
PRIMARY KEY (episode_id, source, recorded_at_ms)
```

Effective reward for an episode is `SUM(value) WHERE episode_id = ?`.

A single `user_score` column on `episode` was rejected: it erases
provenance the moment two reward events stack on one episode (e.g.
direct thumb + synthesis-attributed credit). The trainer corpus needs
the *events*, not just the sum — a `-1` direct + `+0.3` attributed is a
different signal from a `-0.7` critic score, and a future SFT/DPO
pipeline will want to weight or filter by source.

Two columns (`direct_reward`, `attributed_reward`) on `episode` was
also rejected: bakes semantics into column names, requires schema
change for every new reward source, breaks symmetry across sources.

### 2. Attribution rule: additive, not precedence

"Subtask thumb is highest weight" means weight in the
attribution-coefficient sense (1.0 vs. 0.3), not precedence. A subtask
that received a direct thumb-up *and* fractional credit from a
thumbed-up synthesis is summed: `+1.0 + 0.3 = +1.3`. A subtask that
got a direct thumb-down but contributed to a thumbed-up synthesis:
`-1.0 + 0.3 = -0.7`.

The whole point of fractional attribution is that synthesis-level
success/failure carries some signal about upstream contributions.
Precedence-only ("direct thumb wins, attributed credit ignored when
direct exists") throws away that signal exactly when it would be most
useful — when the operator gave both signals.

### 3. "Consumed upstream" = `output_key ∈ consumed_keys`

A new `consumed_keys: JSON list[str]` column on `episode` is populated
by the `workspace_io` tool wrapper on every `.read(key)` call. Set once
at episode close, immutable thereafter.

Synthesis-thumb attribution query:

```sql
SELECT episode_id FROM episode
WHERE task_id = ? AND specialty != 'synthesis'
  AND output_key IN (SELECT json_each.value
                     FROM episode synth, json_each(synth.consumed_keys)
                     WHERE synth.episode_id = ?)
```

Static DAG attribution (credit everyone in `depends_on`) was rejected:
it over-credits researchers whose output synthesis ignored, training
the cluster to keep producing useless outputs. Content matching
(string overlap between synthesis output and upstream output) was
rejected as a brittle heuristic.

The honesty of this rule depends on the contract that `workspace_io`
is the only path to read upstream output. CONTEXT.md commits to that
contract; future tools that bypass `workspace_io` would silently break
attribution.

### 4. 24-hour fallback: nightly sweep, soft scaling

The "no feedback in 24h → critic score is the reward" rule fires in
the same 02:00 nightly job that runs lessons extraction (ADR 0005)
and lessons eviction. Sweep query:

```
INSERT INTO episode_rewards (episode_id, source, value, recorded_at_ms)
SELECT episode_id, 'critic_fallback',
       (critic_score - 0.5) * 0.6,
       <now_ms>
FROM episode
WHERE closed_at_ms < <now_ms - 24h>
  AND critic_status = 'scored'
  AND state != 'REJECTED'
  AND NOT EXISTS (SELECT 1 FROM episode_rewards r
                  WHERE r.episode_id = episode.episode_id
                    AND r.source IN ('subtask_thumb',
                                     'synthesis_thumb_fractional'))
  AND NOT EXISTS (SELECT 1 FROM episode_rewards r
                  WHERE r.episode_id = episode.episode_id
                    AND r.source = 'critic_fallback')
```

The `(critic_score - 0.5) * 0.6` mapping puts critic-fallback values in
`[-0.3, +0.3]` — same magnitude as synthesis-attributed credit, weaker
than a direct thumb. This matches the PRD's "thumb is highest weight"
intent: thumbs are the trunk, critic fallback is a perturbation.

Other normalizations considered:

- `(critic_score * 2) - 1` → `[-1, +1]` (same range as a thumb): makes
  fallback indistinguishable from a strong direct thumb in `value`-space,
  inverting the PRD's weighting intent.
- Raw critic score `[0, 1]`: punts the scaling decision; downstream
  consumers each invent their own and disagree.

Per-episode timer at `closed_at + 24h` was rejected: the in-memory
timer is lost on coordinator restart, and the cluster already has a
nightly window for sweep-style work.

If a thumb arrives later than the fallback fired, both rows coexist
and sum naturally: `critic_fallback +0.18` plus `subtask_thumb +1.0`
= `+1.18`. The fallback is not "overruled"; it remains a recorded
event. Acceptable: the trainer can filter by `source` if it wants
"only direct-feedback episodes."

### 5. Discord-surface mapping

Two new tables on the coordinator:

```
task_messages(message_id PRIMARY KEY, task_id NOT NULL, posted_at_ms)
subtask_threads(thread_id PRIMARY KEY, subtask_id NOT NULL, posted_at_ms)
```

The Discord bot writes one row when it posts the live-DAG message and
one row per subtask thread it creates. On reaction-add, the bot looks
up the surface to resolve `(task_id | subtask_id)` and the
corresponding `episode_id`.

A single polymorphic table with NULL discriminants was rejected on
maintenance grounds — every consumer would need to remember the
NULL-meaning. Two tables, two clear schemas.

### 6. Reconcile-cancel for un-reactions during outages

The Discord gateway does not push reaction events to offline bots.
Coordinator restart, network gap, or Discord outage → reactions added
or removed during the gap are silently lost.

On bot reconnect, a backfill sweep walks the last 48 h of
`task_messages` and `subtask_threads` rows, calls Discord's
`GET /channels/{id}/messages/{id}/reactions/{emoji}` for each, and
reconciles against `episode_rewards`:

- **Reaction present in Discord, absent from `episode_rewards`** → insert
  the missing reward row.
- **Reaction absent from Discord, present in `episode_rewards`** → the
  operator un-reacted during the gap. Insert a *cancellation row* with
  `value` equal to the negation of the prior row's value, same
  `discord_user_id`, same `discord_message_id`.

Cancellation rows preserve the B2 sum-of-rewards semantics: a `+1`
followed by a `-1` cancellation sums to `0`. They also preserve audit
history: a future reader can reconstruct "thumb added, then removed
during outage."

Deleting the prior row was rejected — destroys audit history. Switching
the effective-reward rule to "latest per source" was rejected — it
unwinds the additive-attribution decision in §2.

48 h is the backfill horizon: loose enough to cover any realistic
restart, tight enough that the API sweep stays well inside Discord's
rate limits at typical task volume.

## Consequences

- `EpisodeStore` schema gains one column (`consumed_keys: JSON`); a new
  table `episode_rewards`; two new index tables `task_messages` and
  `subtask_threads`. Migration: existing rows fill `consumed_keys` with
  `[]` (no episodes pre-date workspace_io tracking that matter for
  attribution).
- The `workspace_io` tool wrapper gains a single side-effect: append
  the read key to the current episode's in-progress `consumed_keys`
  buffer. Negligible overhead.
- The Discord bot gains four new responsibilities: write `task_messages`
  on live-DAG message post, write `subtask_threads` on thread create,
  consume reaction-add/remove events, run reconnect backfill. All four
  are coordinator-local.
- The trainer corpus shape changes: `effective_reward` is a derived
  view over the rewards table, not a column. SFT/DPO dataset builders
  must be aware of the join. The trainer-side change is one query.
- Cancellation rows in `episode_rewards` look unusual at first glance
  (negative `value` from a `subtask_thumb` row is a thumb-down; negative
  `value` from a `subtask_thumb` cancellation row is a retracted
  thumb-up). The audit history makes them disambiguatable by ordering;
  consumers that need the live state should use the latest-non-cancelled
  reduction, not the raw stream.
- A future second feedback channel (web UI thumbs in P2 of the
  unified-web-interface PRD) plugs into the same table with a new
  `source` enum value. No schema change.

## Out of scope

- Multi-operator weighting. The PRD assumes solo operator; if
  collaborators are added later, `discord_user_id` becomes load-bearing
  for trust weighting. Not built.
- Reward decay over time (older thumbs count less). Not requested.
- Operator manual override CLI for episode rewards.
- Synchronous reward propagation into a live worker prompt — rewards
  are persisted, consumed by lessons extraction and trainers; they do
  not influence the in-flight worker.
- A "reward dashboard" UI surface. The web-interface P1 PRD covers
  the read-only observability layer; reward visualization is P2+.
