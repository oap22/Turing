# ADR 0003 — Capability token v1

- Status: Accepted
- Date: 2026-05-08
- Closes: #97 (slice spec); reserves the slot defined in ADR 0002

## Context

ADR 0002 locked the `SubtaskDispatch` envelope with a `capability_token`
slot reserved for v1. CONTEXT.md commits to "shell runs on the
coordinator only … one gate, one audit log" with workers emitting signed
`tool_request` callbacks the coordinator gates against a subtask-scoped
capability token.

What was unspecified before this ADR:

- Who verifies the token (issue #97 wording was ambiguous).
- What `cwd` means in the token (free-form path vs sandboxed workspace).
- Whether `timeout` and `expires_at` are one field or two.
- Which key signs tokens (a separate token-issuer key vs the existing
  coordinator MeshMessage signer).
- The wire format for `tool_request` callbacks and the gate's
  verification order.
- v1 policy posture (which specialties get shell access).

This ADR pins all of it before code lands. It's grilled-with-docs output
for #97; the implementation slice references this ADR.

## Decision

### 1. Coordinator verifies; worker presents

The coordinator is the sole verifier. Workers carry the token they
received in `SubtaskDispatch.capability_token` and present it on every
`tool_request` callback. A compromised worker that skips local checks
gains nothing — the gate runs on the coordinator. The issue body's
"worker rejects" wording was a copy-edit slip and has been amended.

### 2. v1 posture: deny-by-default

Every dispatch carries a structurally-valid token, but every
specialty's `allowed_commands_regex` is the unmatchable pattern (`(?!x)x`)
in v1. The full perimeter (issuance, signing, transport, gate, audit) is
exercised end-to-end; no specialty has shell access. Future
specialty-specific allowlists are follow-up issues with their own ADRs
documenting *why this command, why this cwd*.

The reversible choice (regex tightening) is config; the irreversible
choice (perimeter shape) is code. v1 ships the latter.

### 3. `cwd` → workspace-scoped, not a free-form path

The token does not carry a path string. It carries `(task_id,
subtask_id)`; the gate resolves to a managed per-subtask directory under
`workspace://<task_id>/<subtask_id>/`. The coordinator creates the dir
on first `tool_request` for a subtask and reaps it on subtask terminal
transition. The worker cannot influence `cwd` at all.

This kills "operator typo'd a `cwd`" failure modes by construction and
matches the `workspace://...` URI convention already used in DAG
`output_key` / `inputs`.

### 4. `timeout` and `expires_at` are two fields

- `expires_at` — wall-clock millis; token lifetime. Set to
  `dispatch.deadline_ms + 30_000` (matches the dispatch grace window).
  After it elapses, the gate denies regardless of subtask state.
- `timeout` — per-call wall-clock cap, seconds. Default `30`, capped at
  `subtask.timeout_s`. Each individual shell call is bounded by this.

`expires_at` is the JWT-style "is this token still good"; `timeout` is
the per-invocation budget. They enforce different things. Keeping them
separate avoids one runaway command tying up the gate for the whole
subtask window.

### 5. Token signing reuses the coordinator MeshMessage signer

Workers already trust the coordinator's Ed25519 key (used to verify
`SubtaskDispatch`). Splitting into a separate token-issuer key splits
no real trust boundary in a single-coordinator deployment — the same
compromise gives the same powers. One key, one rotation lever, one
trusted-key entry on workers. Multi-coordinator delegation is a v2
concern; trivially additive (workers gain a second trusted key).

### 6. Token shape (v1)

```python
@dataclass(frozen=True)
class CapabilityToken:
    version: int = 1
    subtask_id: str
    task_id: str
    allowed_commands_regex: str   # default: r"(?!x)x"  (matches nothing)
    expires_at_ms: int             # dispatch.deadline_ms + 30_000
    timeout_s: int                 # default 30, ≤ subtask.timeout_s
    issued_at_ms: int
    signature: bytes               # Ed25519 over canonical JSON
```

Workspace is implicit from `(task_id, subtask_id)`; not a token field.

### 7. `tool_request` wire format

Two new subjects:

```
tool_requests.<subtask_id>             # worker → coordinator (signed MeshMessage)
tool_results.<request_id>.result       # coordinator → worker (signed MeshMessage)
```

The token is presented explicitly on every request (the gate could look
it up by `subtask_id` from internal state, but explicit presentation
keeps the audit row self-contained and the wire format
multi-coordinator-ready).

```json
// tool_request payload
{ "version": 1, "request_id": "<uuid>", "subtask_id": "st_...",
  "tool": "shell", "args": {"command": "..."},
  "capability_token": { ...signed token... } }

// tool_result payload
{ "version": 1, "request_id": "<uuid>",
  "status": "ALLOWED" | "DENIED" | "ERROR",
  "exit_code": 0, "output": "...", "stderr": "...",
  "duration_ms": 0, "error": null }
```

v1 routes only `shell` through this path. `vault_query`, `web_fetch`,
`workspace_io` already run on the worker with their own safety
(allowlists, path constraints); routing them adds latency without
changing the threat model. New HIGH-risk tools opt in by routing here.

Replay protection: `request_id` (uuid) + the existing
`transport.envelope.ReplayWindow`. No subtask-level idempotency on
shell calls — re-running denied is fine; re-running allowed is the
worker's bug.

### 8. Verification algorithm at the gate

Steps run in order. The first failing step yields the row's outcome.

1. Verify MeshMessage Ed25519 signature on the `tool_request` frame.
   Fail → audit `DROPPED (signed-frame-invalid)`, drop. **No reply.**
2. ReplayWindow check on `request_id`.
   Fail → audit `DROPPED (replay)`, drop. **No reply.**
3. Verify Ed25519 signature on the `capability_token` (coordinator pubkey).
   Fail → audit `DENIED (token-signature)`, reply `DENIED`.
4. `token.subtask_id == request.subtask_id` AND subtask state is
   `RUNNING` (per the lifecycle log).
   Fail → audit `DENIED (subtask-mismatch | subtask-not-running)`, reply
   `DENIED`. **Subtask-state check precedes expiry** — a
   FAILED/COMPLETED subtask must not accept callbacks even within token
   lifetime.
5. `now_ms < token.expires_at_ms`.
   Fail → audit `DENIED (expired)`, reply `DENIED`.
6. `re.fullmatch(token.allowed_commands_regex, request.args.command)`.
   **`fullmatch`, not `match`** — `match` only anchors the start, which
   is a classic regex-bypass footgun (`^cat ` allows `cat foo; rm -rf /`).
   Fail → audit `DENIED (command-not-allowed)`, reply `DENIED`.
7. Resolve workspace path under `workspace://<task_id>/<subtask_id>/`;
   create it if absent.
8. Execute shell with `cwd=<workspace>`, `timeout=token.timeout_s`.
   On exit / timeout / error → audit `ALLOWED` with `exit_code` and
   `duration_ms`, reply `ALLOWED` with output.

### 9. Audit row shape

Extends the existing audit log table. Columns:

```
ts_ms, subtask_id, worker_id, request_id, tool, command,
outcome ∈ {DROPPED, DENIED, ALLOWED}, reason, exit_code, duration_ms,
stdout_truncated_bytes, stderr_truncated_bytes, token_fingerprint
```

`token_fingerprint = sha256(token_bytes)[:16]` — pins which exact token
was presented without storing its full contents.

### 10. Revocation

TTL only in v1, per `expires_at_ms`. No revocation list, no
explicit-revoke API. If a token must be revoked early, the operator
transitions the subtask to FAILED — step 4 of the gate then denies
every subsequent callback. Explicit revocation is a v2 concern.

## Out of scope

- Per-specialty allowlists with non-empty regexes — each is a follow-up
  issue + ADR. v1 is structurally permissive, policy-deny.
- Multi-coordinator token issuance and trust delegation.
- Routing `vault_query` / `web_fetch` / `workspace_io` through the gate.
- A revocation list / explicit revoke API.

## Consequences

- The `capability_token` slot in `SubtaskDispatch` becomes mandatory at
  the orchestrator layer (every dispatch issues one). Workers without
  the v1 token format are rejected at the gate, not at dispatch.
- The shell tool on workers is removed from the local registry; workers
  cannot invoke shell except through the callback path. (The local
  shell tool stays on the coordinator for non-worker contexts — admin
  CLI, etc.)
- The audit log gains the seven new columns above. Existing rows fill
  with NULL on migration; the new path always writes them.
- A future "this specialty needs `git log`" decision lands as a small
  config change to that specialty's regex plus a one-page ADR. The
  perimeter doesn't move.
