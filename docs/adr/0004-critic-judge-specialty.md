# ADR 0004 — Critic dispatched as a `judge` specialty over NATS

- Status: Accepted
- Date: 2026-05-08
- Relates to: Phase 0 → Phase 1 transition; closes the runtime gap between `learning/critic/` library code and the `EpisodeStore` it is supposed to score.

## Context

The `learning/critic/` package ships with a working `CriticQueue` (queue.py),
local + calibration `Critic` interface, and `DriftDetector`, all unit-tested.
None of it is wired into the runtime: terminal episodes are written to
`EpisodeStore` with `critic_score=NULL` and stay that way. Closing this gap
is the keystone of "close the learning loop" — without critic scores, the
lessons extractor has nothing to rank by, and the reward-attribution
fallback ("no Discord feedback in 24h → critic score is the reward") has
nothing to fall back to.

The current `CriticQueue._score_one` calls `self._local.score(episode)`
directly — assumes the critic runs in the same process. Two facts make
that incompatible with the deployed cluster:

1. The coordinator runs on a Pi 5 (8 GB). It cannot host the 13B+ critic
   the PRD calls for.
2. CONTEXT.md and the multi-edge PRD already define the MBP as a
   `judge`-specialty premium worker. The critic *is* a worker, by design.

What this ADR pins:

- Where the critic physically runs.
- How the coordinator hands an episode to it.
- What happens when the worker is offline, slow, or wrong.

## Decision

### 1. The critic is a `judge` specialty worker on the MBP

The coordinator's `CriticQueue` does not call a Python `Critic` directly.
It dispatches each episode as a subtask to a worker advertising
specialty `judge`, using the existing `SubtaskDispatchClient` shipped in
slices #93–#97. The result returns through the standard subtask result
path; the coordinator writes `critic_score` (and, for the calibration
sample, the cloud score for `DriftDetector`) back to `EpisodeStore`.

Reuse over invention: the dispatch + signing + result path is the second
customer of the abstraction we just built, not a parallel transport.

### 2. The `Critic` interface stays; a `RemoteCritic` adapter wraps dispatch

The in-tree `Critic` Protocol is unchanged. A new `RemoteCritic`
implementation wraps `SubtaskDispatchClient` and conforms to the same
interface. Unit tests against `Critic` keep working with in-process
fakes; an integration test exercises `RemoteCritic` against a fake judge
worker.

`CriticQueue` does not learn about NATS. It still takes a `Critic`.

### 3. Episodes travel inline, truncated at dispatch time

The `EpisodeStore` stays canonical: full untruncated trajectories are
written there for the trainer corpus. Truncation is applied at the
dispatch boundary only — head + tail per trajectory step (2KB / 2KB),
final output (8KB / 8KB), tool args/results (1KB / 1KB). The judge
worker scores the truncated view.

The critic scores behavior shape, not byte-perfect transcripts; the
trainers want the full transcript. Different consumers, different
budgets — centralize the truncation at the dispatch boundary, not at
write time.

NATS object-store handoff for episodes that exceed 1 MB after truncation
is deferred. No real episode in the corpus today comes close.

### 4. Terminal states scored: COMPLETED, FAILED, TIMED_OUT

REJECTED is excluded. PRD line: REJECTED is "captured for audit but
excluded from positive training data to prevent teaching the model to
evade safety." Critic scores feed lessons (top-K extraction) and the
reward-attribution fallback; if a REJECTED episode lands in top-K, you
have back-doored the safety bypass into future system prompts.

The critic prompt is written to handle "no output" gracefully — a
TIMED_OUT or FAILED subtask with a partial trajectory and no final
output gets a low score and a critique that references the failure
mode. The negative signal exists; the corpus tells the truth.

### 5. Backfill of unscored terminal episodes

The in-memory queue is lost on coordinator restart, and the MBP can be
offline. Two failure modes both leave terminal episodes with
`critic_score=NULL` indefinitely.

Backfill runs on two triggers:

1. Coordinator startup.
2. Judge-worker registration event (CapabilityRegistry).

Query: `critic_score IS NULL AND is_terminal AND state != REJECTED AND
closed_at > now - 24h AND critic_status IS DISTINCT FROM 'failed'`.
All matching episodes are enqueued.

Live arrivals are FIFO with backfill — they do not jump the queue.
Backlog stragglers tolerate a few extra minutes of staleness; live
episodes do not need a priority lane (one queue, simple model).

The 24-hour bound matches the timescale of the runtime consumers
(lessons extraction, reward fallback). Older episodes are the trainer's
problem; the trainer can re-score the corpus in batch when assembling
datasets.

### 6. Failure handling at the gate of the coordinator

The judge worker can fail in several ways: LLM error, malformed score
JSON, subtask timeout, worker crash mid-score.

- Retries: 3 attempts with 30 s / 2 m / 10 m backoff. Coordinator-side,
  not worker-side — the dispatch path is the natural retry boundary,
  and a worker-side retry cannot recover from "worker crashed."
- After exhaustion: a new `critic_status` column is set to `'failed'`.
  `critic_score` stays NULL.
- The backfill query (above) excludes `critic_status='failed'`, so a
  poison-pill episode does not re-enter the queue on every restart.
- `critic_status='failed'` is permanent in v1. An admin rescore CLI
  (`python -m turing.critic rescore --failed`) is deferred to v2 — keep
  slice 1 small.

This distinguishes the two states that A's "leave NULL" approach
conflates: "haven't scored yet" (eligible for backfill) vs. "tried,
gave up" (skipped).

## Consequences

- The coordinator gains a hard runtime dependency on a `judge`-specialty
  worker registering. With no judge online, every terminal episode
  accumulates as `critic_score=NULL`. This is the intended degradation:
  the cluster keeps running, the corpus keeps growing, the runtime
  feedback loop pauses until a judge comes back.
- `EpisodeStore` schema gains one column: `critic_status`. Existing rows
  fill with NULL on migration; the new path writes `'pending'` →
  `'scored'` | `'failed'`.
- `RemoteCritic` becomes the second consumer of `SubtaskDispatchClient`
  in production. Any breakage in the dispatch path now blocks both the
  primary task spine and the learning loop — that is correct (one
  transport, one bug surface) but raises the stakes of regressions in
  `coordinator/dispatch/`.
- The MBP `judge` worker must register its specialty in the capability
  manifest. Slice 1 includes the worker-side bring-up.
- A future "second judge worker" (e.g. a Jetson hosting a 7B critic for
  redundancy) is trivially additive: it advertises `judge`, the
  scheduler tie-breaks on eval score. No wire-format change.
- Reversal cost: moving the critic back in-process is a one-week refactor
  that touches the dispatch path, the queue, and the worker-side
  registration. Plan around the assumption it stays remote.

## Out of scope

- NATS object-store handoff for episodes that exceed the inline budget
  after truncation. Add when a real episode hits the limit.
- Multi-judge load balancing beyond the existing scheduler tie-break.
- Admin rescore CLI for `critic_status='failed'` episodes.
- Synchronous "score now" path for ad-hoc scoring outside the lifecycle
  hook (e.g. backfilling old corpora). Trainers do their own scoring.
