# ADR 0001: DAG schema and planner contract

- **Status:** Proposed
- **Date:** 2026-05-07
- **Slice:** #8 — child of PRD #2
- **Covers user stories:** 16, 18 (and informs 11, 12, 17, 32)

## Context

PRD #2 reshapes Turing into a coordinator/worker cluster where the coordinator
decomposes non-trivial tasks into a typed DAG of subtasks dispatched to
specialty workers (US 16). Workers may also discover, mid-execution, that a
piece of work falls outside their domain and ask the coordinator to extend the
DAG via a `needs_subtask` response (US 18). Trivial single-specialty tasks
bypass the planner via a cheap classifier (US 17).

Before any planner code lands (slice #9), we must lock:

1. The **DAG JSON schema** the planner emits.
2. The **`needs_subtask`** worker→coordinator response shape.
3. The **planner routing rules** — when does each task hit the trivial
   classifier, the local 13B planner on the MBP, or Claude Sonnet?

These shapes are the contract every downstream slice depends on (scheduler #11,
retry #12, workspace addressing #19, untrusted-data wrapping #32, lifecycle
events #14). Changing them later means changing every consumer, so we lock
them now.

## Decision

### 1. DAG schema

A DAG is a flat list of `Subtask` objects with declared dependencies. No nested
structure, no implicit ordering. The list is topologically sortable from
`depends_on` references alone.

```json
{
  "task_id": "tsk_01HX...",
  "version": 1,
  "subtasks": [
    {
      "id": "st_a",
      "specialty_required": "research-discover",
      "prompt": "Find the 5 most-cited 2025 arxiv papers on speculative decoding.",
      "depends_on": [],
      "required_tools": ["web_fetch", "workspace_io"],
      "inputs": {},
      "output_key": "workspace://tsk_01HX.../st_a/result",
      "max_retries": 1,
      "timeout_s": 120
    },
    {
      "id": "st_b",
      "specialty_required": "research-summarize",
      "prompt": "Summarize the discoveries written to {{ inputs.papers }} into 3 paragraphs that match the operator's vault voice.",
      "depends_on": ["st_a"],
      "required_tools": ["vault_query", "workspace_io"],
      "inputs": { "papers": "workspace://tsk_01HX.../st_a/result" },
      "output_key": "workspace://tsk_01HX.../st_b/result",
      "max_retries": 1,
      "timeout_s": 180
    }
  ]
}
```

**Field semantics — locked:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `task_id` | str (ULID, prefix `tsk_`) | yes | Top-level task; same prefix used in workspace URIs and JetStream subjects. |
| `version` | int | yes | Schema version. **`1`** for this ADR. Bump on breaking changes. |
| `subtasks` | list[Subtask] | yes | Min 1 entry. Validation rejects cycles and dangling `depends_on`. |
| `subtasks[].id` | str (`st_*` slug) | yes | Unique within a DAG. Stable across retries. |
| `subtasks[].specialty_required` | enum `Specialty` | yes | Must match a value the registry knows. See "Specialty enum" below. |
| `subtasks[].prompt` | str | yes | Mustache-style `{{ inputs.<key> }}` substitution evaluated at dispatch. |
| `subtasks[].depends_on` | list[str] | yes (may be `[]`) | Each entry must be the `id` of another subtask in the same DAG. |
| `subtasks[].required_tools` | list[str] | yes (may be `[]`) | Names from the tool registry. Scheduler (US 11) refuses to dispatch unless `required_tools ⊆ worker.manifest.tools`. |
| `subtasks[].inputs` | dict[str, str] | yes (may be `{}`) | Map of mustache key → workspace URI. URIs must reference an `output_key` of a subtask in `depends_on` (validation enforces this). |
| `subtasks[].output_key` | str | yes | Workspace URI where the worker writes its result. Convention: `workspace://<task_id>/<subtask_id>/result`. The schema only enforces the `workspace://` prefix; the convention is the planner's job. |
| `subtasks[].max_retries` | int (0–3) | no, default `1` | Per slice US 12 — one retry to a different worker is the policy. Schema permits 0–3 to allow future tuning. |
| `subtasks[].timeout_s` | int (5–1800) | no, default `120` | Per-subtask wallclock. |

**`Specialty` enum** (locked for v1; matches PRD per-specialty roster):
`research-discover`, `research-deep`, `research-summarize`, `synthesis`,
`judge`, `planner`. Adding a specialty is **not** a breaking change (workers
that don't know it simply never get dispatched to). Removing one is.

**Validation rules** (Pydantic-enforced):

- `subtasks` is non-empty.
- All `id`s are unique.
- Every entry in `depends_on` resolves to another `id` in the same DAG.
- The graph is acyclic (DFS check).
- Every workspace URI in `inputs` references the `output_key` of one of the
  current subtask's transitive dependencies. (We enforce *direct* dependency
  reference; transitive dependencies must be made explicit via `depends_on`.)
- `required_tools` strings match a known tool name (validated at dispatch, not
  at parse time, so the schema isn't tightly coupled to the live tool
  registry).

**Why a flat list with `depends_on` instead of nesting?**

A flat list is trivially serialisable, lets the scheduler (#11) build a ready
queue with one topological sort, and matches how lifecycle events
(`tasks.<task_id>.events`) project state — one event per subtask transition.
Nested structures would require flattening at every consumer.

### 2. `needs_subtask` response shape

When a worker returns `status: needs_subtask`, the body is a **partial** DAG
fragment, not a single subtask. This lets a worker request a small chain
("first discover, then deep-research these papers") in one round-trip.

```json
{
  "status": "needs_subtask",
  "reason": "I am research-summarize; the operator asked for a literature search which requires research-deep first.",
  "fragment": {
    "subtasks": [
      {
        "id": "st_a_sub1",
        "specialty_required": "research-deep",
        "prompt": "Read the top arxiv papers on X and produce a structured findings doc.",
        "depends_on": [],
        "required_tools": ["web_fetch", "workspace_io"],
        "inputs": {},
        "output_key": "workspace://<parent_task_id>/st_a_sub1/result"
      }
    ],
    "rejoin_after": ["st_a_sub1"]
  }
}
```

**Coordinator behaviour:**

- The coordinator validates `fragment.subtasks` with the same rules as a top-level
  DAG, prefixed under the requesting subtask's id (e.g., `st_a` becomes the
  parent of `st_a_sub1`).
- The original requesting subtask transitions `RUNNING → NEEDS_SUBTASK` (per
  PRD lifecycle), then re-enters `PENDING` with `depends_on` extended to
  include all ids in `fragment.rejoin_after`.
- Workers do **not** sub-delegate directly (no worker-to-worker dispatch). The
  coordinator owns DAG mutation. This keeps the lifecycle machine the single
  source of truth.

**Why a fragment, not a single subtask?**
A worker might genuinely need a 2-step chain (discover → deep). Forcing
multiple round-trips would add latency and noise. The fragment is bounded —
schema validation rejects fragments larger than 5 subtasks (configurable) to
prevent runaway expansion.

### 3. Planner routing rules

Three planning paths, picked in order:

```
                         ┌──> trivial classifier  ──> 1-subtask DAG (no LLM cost)
incoming task ──> route ─┼──> local 13B planner   ──> N-subtask DAG (cheap)
                         └──> Claude Sonnet       ──> N-subtask DAG (cloud spend)
```

**Rule 1 — Trivial classifier (US 17).** A small embedding-based or
keyword classifier emits one of two answers: `(specialty, confidence)` or
`unknown`. If `confidence ≥ 0.85` and the matched specialty has at least one
healthy worker, the planner short-circuits to a single-subtask DAG with that
specialty and the user's prompt verbatim. No LLM call. This is the path that
makes "summarise this article" cost zero planning tokens.

**Rule 2 — Local 13B planner.** When the trivial classifier returns
`unknown` or low confidence, route to the local 13B planner on the MBP
(`synthesis`/`planner` specialty). The local planner runs unless any of the
following Sonnet-trigger conditions hold.

**Rule 3 — Claude Sonnet.** Used when the local planner is insufficient.
Triggers (any one is enough):

- The MBP is offline (no `planner`-capable worker registered).
- The task prompt's token count exceeds `local_planner_max_input_tokens`
  (default 8 000). The local 13B's effective planning context is smaller than
  Sonnet's; oversized prompts go straight to cloud.
- The local planner returned a malformed DAG (failed schema validation) on
  first attempt. We retry once on Sonnet rather than loop locally.
- The user explicitly tagged the request `#deep` in Discord.
- `force_cloud_planner` config flag is set (escape hatch).

The budget gate (US 53) sits in front of every Sonnet call — if remaining
daily budget can't cover the estimated cost, the planner degrades to the local
13B and emits a Discord notice. **The budget gate's "no" is final**; we do not
try to upgrade back to Sonnet within a task.

**Why these specific rules?**

- The trivial classifier handles the dominant traffic shape (single-specialty
  asks) at ~zero cost. PRD's $10/day budget makes this non-negotiable.
- The local 13B is "good enough" for 2–4 subtask decompositions, which is
  what most multi-step research tasks look like. We measured nothing yet —
  this threshold is a starting point we will revisit after one week of
  episode data shows actual planner-quality distributions.
- Sonnet is reserved for genuine complexity and oversized inputs, the two
  cases where local 13B reliably fails today.

### What we are *not* deciding here

- **Tool registry contents.** The set of valid `required_tools` strings is
  owned by the tool registry, not this ADR.
- **Specialty registration / health.** Out of scope; lives in the worker
  registry (slice #11).
- **Workspace storage backend.** The schema only knows about `workspace://`
  URIs as opaque strings. Object-store details belong to slice #10.
- **Critic / episode shape.** Separate ADR when it lands (post slice #19/#20).

## Consequences

**Good.**

- Slice #9 (planner → DAG → dispatch → synthesis) has a frozen target schema;
  it can be built and tested against fixtures without coordination drift.
- The scheduler (slice #11) and lifecycle (slice #14) can pin to v1 of the
  schema and treat it as a stable contract.
- Pydantic models give us free runtime validation at every coordinator
  ingress (planner output, `needs_subtask` responses, persisted DAG re-loads
  on coordinator restart).
- `version: 1` field means future breaking changes are explicit and
  observable in stored events.

**Bad / accepted.**

- Locking the specialty enum at v1 means adding e.g. `code-review` later
  requires a v2 bump or treating the enum as open. We accept this — closed
  enums are easier to reason about; we'll re-evaluate if specialty churn
  becomes painful.
- The planner-routing thresholds (`0.85` classifier confidence,
  `8000`-token local cap) are guesses from the PRD. They will likely change
  after a week of telemetry. They live in config, not the ADR — only the
  *rule structure* is locked here.
- The "fragment ≤ 5 subtasks" bound on `needs_subtask` is arbitrary.
  Configurable; revisit if real workloads bump into it.

**Follow-ups.**

- Add `coordinator/planner/schema.py` (this slice's AC).
- Add `coordinator/planner/fixtures/` with one example per path (trivial,
  multi-subtask, `needs_subtask`).
- Slice #9 consumes both. Slice #11's scheduler keys off
  `required_tools` ⊆ `manifest.tools` exactly as defined here.
