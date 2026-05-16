# PRD: Unified web interface to the agent fleet (P1: read-only observability)

> GitHub issue: [#38](https://github.com/oap22/Turing/issues/38)
> Label: `ready-for-agent`
> Slices: [#39](https://github.com/oap22/Turing/issues/39) · [#40](https://github.com/oap22/Turing/issues/40) · [#41](https://github.com/oap22/Turing/issues/41) · [#42](https://github.com/oap22/Turing/issues/42) · [#43](https://github.com/oap22/Turing/issues/43) · [#44](https://github.com/oap22/Turing/issues/44) · [#45](https://github.com/oap22/Turing/issues/45) · [#46](https://github.com/oap22/Turing/issues/46) · [#47](https://github.com/oap22/Turing/issues/47)

## Problem Statement

Today, the only user-facing surface for the Turing fleet is Discord, and only pi-alpha holds the Discord token — nodes 2–4 are silent workers. When something goes wrong (latency spike, a node looks idle, a tool call hangs, the LLM router falls back to cloud unexpectedly), there is no way to see *what the fleet is actually doing* without SSH'ing into each Pi and tailing structlog. The user has no single interface to communicate with all of the agents, and no diagnostic view into how messages, LLM calls, tool dispatches, and memory queries are flowing across the mesh.

The end-state vision is a team-grade web app that becomes the single interface to the fleet — chat plus a live layered call-graph plus message trace plus telemetry — replacing Discord. This PRD covers **Phase 1 only: read-only observability**. Discord remains the primary chat surface during P1. P2 (web chat replaces Discord, magic-link auth) and P3 (multi-user team features, requester-only HIGH-risk approvals) will be specified in their own PRDs.

## Solution

Pi-alpha hosts a new gateway (FastAPI + WebSocket + SQLite ring buffer) running as additional asyncio tasks inside the existing turing process. Every node in the mesh emits structured telemetry events through a central `Telemetry` event bus, fed by `@traced` decorators on the hot paths in `llm/`, `tools/`, `memory/`, `learning/`, and the agent loop. Events ship over a Zyre SHOUT group; pi-alpha drains them into a 24-hour, 500MB-capped SQLite ring buffer and fans them out live to connected web clients.

A React + Vite + React Flow + Tailwind SPA, served as static assets from the gateway, renders a layered call-graph: Pi-nodes in a left column, their tools and memory stores in the middle, LLM providers (Claude, Ollama) on the right. Edges pulse in real time with latency. A message-trace pane shows recent inter-node messages and intra-node LLM/tool calls with truncated, redacted prompt samples. The `turing` CLI is a thin launcher: it resolves pi-alpha, sets an auth cookie from a static bearer token, and opens the browser. The gateway binds only to the Tailscale interface and requires the bearer token.

## User Stories

1. As a fleet operator, I want a single URL I can bookmark to see the entire agent fleet, so that I do not have to SSH into each Pi to understand what is happening.
2. As a fleet operator, I want to run `turing` on my laptop, so that the CLI resolves pi-alpha and opens the web UI in my browser without me memorizing the URL.
3. As a fleet operator, I want the gateway to bind to my Tailscale interface only, so that the UI is not exposed to the broader network.
4. As a fleet operator, I want a static bearer token check on every WebSocket connection, so that a leaked Tailscale ACL alone cannot grant access.
5. As a fleet operator, I want to see all four Pi-nodes as graph nodes in fixed left-column slots, so that my eye builds muscle memory for which node is which.
6. As a fleet operator, I want each Pi-node to display its current LLM provider, active tools, and memory store as connected sub-nodes, so that I can see the agent's substrate at a glance.
7. As a fleet operator, I want graph edges to pulse with live latency numbers when an LLM call is in flight, so that I can see which calls are slow as they happen.
8. As a fleet operator, I want the graph to update in real time without manual refresh, so that I am always looking at the current state.
9. As a fleet operator, I want a message-trace pane listing recent mesh messages and intra-node calls with timestamps and durations, so that I can correlate a graph spike with the specific call that caused it.
10. As a fleet operator, I want sampled prompts and responses attached to each LLM call entry in the trace, so that I can debug bad LLM behavior without re-running the request.
11. As a fleet operator, I want prompt/response samples truncated to 2KB head and tail with a clear truncation marker, so that the ring buffer cannot OOM the Pi.
12. As a fleet operator, I want emails, API keys, and bearer tokens redacted from captured prompts before they reach the ring buffer, so that secrets do not leak into telemetry storage.
13. As a fleet operator, I want to scrub the graph back up to 24 hours, so that I can diagnose an incident I noticed the next morning.
14. As a fleet operator, I want the ring buffer to FIFO-evict the oldest events when it hits 500MB, so that SD-card wear and disk usage are bounded.
15. As a fleet operator, I want the ring buffer to survive pi-alpha process restarts, so that a crash during an incident does not erase the evidence.
16. As a fleet operator, I want each emitted event to carry a per-stream monotonically increasing sequence number, so that the gateway can detect drops.
17. As a fleet operator, I want the UI to render a visible "N events dropped" pill on any node where a sequence-number gap was detected, so that I am never silently lying to myself about what happened.
18. As a fleet operator, I want critical events (errors, safety-gate denials) to use TCP-WHISPER fallback rather than UDP SHOUT, so that the most diagnostically important events are never dropped.
19. As a fleet operator, I want each node's `Telemetry.emit` calls to be non-blocking, so that telemetry overhead never stalls the agent loop.
20. As a fleet operator, I want a `@traced` decorator I can apply to a hot-path method to auto-emit start/end/duration/error events, so that adding instrumentation is one line.
21. As a fleet operator, I want LLM calls in `llm/router.py` to be traced, so that I can see local vs. cloud routing decisions and per-provider latency.
22. As a fleet operator, I want tool dispatches in `tools/registry.py` to be traced, so that I can see which tools are slow or failing.
23. As a fleet operator, I want memory queries in `memory/retriever.py` to be traced, so that I can spot vector-search latency regressions.
24. As a fleet operator, I want learning/pattern-extraction runs in `learning/` to be traced, so that I can see when periodic extraction is firing and how long it takes.
25. As a fleet operator, I want the agent loop iterations in `agent/core.py` to be traced, so that I can see how many tool-call iterations a given message took.
26. As a fleet operator, I want safety-gate decisions to be traced as priority events, so that every APPROVED/DENIED/NEEDS_CONFIRMATION outcome is auditable in the UI.
27. As a fleet operator, I want the gateway to be configurable via existing `TURING_`-prefixed env vars, so that it follows the same configuration convention as the rest of the system.
28. As a fleet operator, I want to disable the gateway entirely via a single env var, so that nodes other than pi-alpha do not waste cycles on it.
29. As a fleet operator, I want the gateway to start and stop cleanly with the rest of the turing process, so that I do not need a second systemd unit.
30. As a fleet operator, I want my P1 changes to leave Discord untouched and primary, so that I keep my chat surface while validating the telemetry pipeline.
31. As a fleet operator, I want the React frontend bundle served as static files by the gateway, so that there is no second web server to deploy.
32. As a fleet operator, I want graph layout fixed by role (Pi-nodes left, tools/memory middle, LLM providers right), so that the visual structure does not jitter as nodes appear and disappear.
33. As a fleet operator, I want to filter the message-trace pane by node, by event type, and by minimum duration, so that I can isolate the slow calls during an incident.
34. As a fleet operator, I want the auth bearer token rotated by editing one env var and restarting the gateway, so that compromised tokens are easy to revoke.
35. As a fleet operator, I want the `turing` CLI to fail loudly if it cannot resolve pi-alpha or authenticate, so that I never sit in front of a blank browser tab wondering what is wrong.

## Implementation Decisions

### Architecture

- The gateway runs **inside the existing turing process** on pi-alpha as additional asyncio tasks (FastAPI/uvicorn, Zyre subscriber, ring-buffer pruner). It is gated by a config flag so nodes 2–4 do not spin it up.
- The gateway exposes **one WebSocket endpoint** carrying bidirectional JSON frames. Server pushes `peer_update`, `message_trace`, `metric`, and `gap_marker` frames. Client sends nothing in P1 beyond a subscribe frame (chat is P2).
- The CLI (`turing` command) is a thin launcher: it resolves pi-alpha (config or mDNS), sets an auth cookie containing the bearer token, and opens the browser. It is not a TUI.
- The gateway **binds to the Tailscale interface only** (configurable). Token auth is required on the WS handshake and on the static-asset routes.

### New modules

- **`telemetry/` event bus** — singleton `Telemetry` with `emit(event)` and a `@traced(event_name)` decorator. Auto-emits start/end/duration/error events with monotonically increasing per-stream sequence numbers. Non-blocking emit (puts onto an asyncio queue; a background task drains to Zyre). Deep module: a handful of methods, internals (sequencing, queue management, transport selection by priority) hidden.
- **`telemetry/redactor`** — pure function that runs regex-based redaction (emails, API keys, bearer tokens, common secret patterns) and head+tail truncation to 2KB total. Trivially unit-testable.
- **`gateway/`** — FastAPI app, WS connection manager, bearer-token middleware, static-asset routes for the SPA bundle.
- **`gateway/ring_buffer`** — SQLite-backed event store. `append(event)`, `query(filter, since)`, `prune()`. TTL of 24 hours, FIFO eviction at 500MB cap. Reuses the existing aiosqlite + alembic stack.
- **`gateway/telemetry_sink`** — Zyre subscriber. Drains the telemetry SHOUT group (and TCP-WHISPER for priority events) into the ring buffer, detects per-stream sequence gaps, broadcasts `gap_marker` frames to live WS clients.

### Modified modules

- **`mesh/protocol.py`** — add `MessageType.TELEMETRY` and `MessageType.TELEMETRY_PRIORITY`. Priority events take the TCP-WHISPER path so they survive UDP loss.
- **`mesh/node.py`** — every node joins a "telemetry" SHOUT group; pi-alpha's gateway subscribes to it. WHISPER fallback for priority events.
- **`llm/router.py`, `tools/registry.py`, `memory/retriever.py`, `learning/*`, `agent/core.py`, `agent/safety.py`** — add `@traced` decorators on hot-path methods (~5–7 emission sites total). Safety-gate decisions emit at priority.
- **`__main__.py`** — initialize and start the gateway as an additional asyncio task on pi-alpha (after MeshNode, before Agent).
- **`config.py`** — new env vars (Pydantic Settings, `TURING_` prefix): `TURING_GATEWAY_ENABLED`, `TURING_GATEWAY_TOKEN`, `TURING_GATEWAY_BIND`, `TURING_GATEWAY_PORT`, `TURING_TELEMETRY_RETENTION_HOURS`, `TURING_TELEMETRY_MAX_BYTES`, `TURING_TELEMETRY_PROMPT_SAMPLE_MAX_BYTES`.

### New frontend

- **`webui/`** — Vite + React + React Flow + Tailwind SPA. Built into static assets served by the gateway. Three panes: layered call-graph, message trace (filterable by node/event-type/min-duration), live metrics summary. Read-only in P1. Layout: fixed columns by role (Pi-nodes left, tools/memory middle, LLM providers right), auto-layout within each region. Custom React Flow node renderers per role with mini-stats inside each node.

### Event schema

- Every event carries: `node_id`, `node_name`, `event_type` (e.g. `llm.call.start`, `tool.dispatch.end`, `mesh.send`, `safety.gate.decision`), `seq` (monotonic per node-per-stream), `ts`, `priority` (`normal`|`high`), and a typed `payload`.
- LLM call events carry sampled, redacted, truncated prompt and response slices.
- Tool events carry tool name, sampled redacted args, outcome.
- Memory events carry query type and timing.
- Mesh events carry peer ID and message type.
- Safety-gate events carry the decision and rule that fired (always priority).

### Deployment / config

- Single Python process on pi-alpha (per CLAUDE.md's initialization order, gateway slots in after MeshNode and before Agent).
- Frontend bundle is built at packaging time and shipped inside the wheel.
- `TURING_GATEWAY_ENABLED=true` only on pi-alpha; defaults false elsewhere.

## Testing Decisions

Tests target external behavior, not implementation details. A good test exercises the public interface of a module against a realistic input and asserts on observable outcomes (returned values, emitted events, persisted rows, frames sent to a WS client) — never on private method calls or internal state shapes that are free to refactor. The repo already follows this style: tests use in-memory SQLite (`:memory:`), mocked LLM providers returning canned `LLMResponse` objects, and config fixtures with `_env_file=None`. Reuse those patterns.

### Modules with tests

1. **`telemetry/` event bus + `@traced` decorator + `redactor`**
   - On a successfully-completing traced function, a start event and an end event with non-zero duration are emitted in order with monotonic seq numbers.
   - On a raising traced function, a start event and an error event with the exception type are emitted; the exception still propagates.
   - Emits are non-blocking (queue accepts even when downstream is paused).
   - Redactor strips email-shaped, bearer-token-shaped, and API-key-shaped substrings; truncates to the configured byte budget with a clear marker.
   - Prior art: existing `tests/test_tools/test_shell.py` for unit tests over pure logic; existing agent loop tests for decorator-style behavior verification.

2. **`gateway/ring_buffer` (TTL + size-cap)**
   - Appended events are queryable by filter (node, event_type, time range).
   - When the configured TTL passes, old events are evicted by `prune()`.
   - When total size exceeds the cap, oldest rows are FIFO-evicted on append.
   - Survives an in-process restart (close + re-open the SQLite handle, prior events still queryable).
   - Prior art: any existing memory-store / SQLite-backed tests in `tests/test_memory/`.

3. **`gateway/telemetry_sink` gap detection**
   - Given a stream of events with seq numbers `1, 2, 4, 5`, a gap marker for the missing seq `3` is emitted to subscribed WS clients.
   - Out-of-order arrival of `1, 3, 2, 4` does not trigger a false gap.
   - A reset/restart of a node (seq returns to 1) is detected as a stream reset, not a gap.

4. **Gateway WS auth + end-to-end smoke**
   - WS handshake without bearer token → 401.
   - WS handshake with wrong token → 401.
   - WS handshake with correct token → upgraded; subscribe frame returns initial peer state.
   - End-to-end: spin up the gateway with a fake mesh peer that emits canned telemetry events; assert the corresponding `message_trace` and `metric` frames arrive at a connected test WS client within a small bounded delay.

## Out of Scope

- **Phase 2 (web chat replaces Discord)** — magic-link auth, browser chat surface, smart-routing, `@node` and `@all` semantics, parallel concurrent agent runs in shared timeline, rebuilt tool-confirmation UI, memory wipe at Discord retirement. Specified in a separate PRD.
- **Phase 3 (team polish)** — multi-user roles, requester-only HIGH-risk tool approvals with admin override, per-user memory keys within shared workspace. Specified in a separate PRD.
- Mobile / off-LAN access beyond Tailscale.
- Push notifications.
- Long-term telemetry retention beyond 24 hours.
- Per-node SQLite or distributed querying — pi-alpha is the single aggregation point.
- Reliable delivery for non-priority events — best-effort with visible gap markers is the contract.
- LLM-based routing of any kind in P1 (router lives in P2).
- Multi-tenant or multi-team — single team, shared workspace assumed.
- Discord-to-web message mirroring.

## Further Notes

- **Phasing rationale:** the user explicitly chose phased delivery (observability → chat → team) to validate the telemetry pipeline and graph rendering before also having to rebuild auth, the tool-confirmation UI, and the smart-router. Each phase is independently shippable.
- **Costs the user has explicitly accepted for the broader vision:** rebuilding the Discord button-confirmation flow in the web UI (P2), SMTP/Resend dependency on pi-alpha (P2), Tailscale or equivalent for off-LAN access, wipe of existing Discord-keyed memory at the P2 cutover.
- **Privacy posture:** prompts are sampled at 100% but truncated to 2KB total per side and run through the regex redactor before storage. The redactor is the last line of defense; future iterations could add an opt-in "redact aggressively" mode or per-event opt-out.
- **Pi resource budget:** 4 nodes × ~50 events/sec peak × ~200B per row → ~17M rows/day uncapped. The 500MB ring-buffer cap with FIFO eviction keeps SD-card wear bounded. If sustained event volume turns out to be higher in practice, raise the truncation limit downward before raising the size cap.
- **CLAUDE.md initialization order:** the gateway slots in after MeshNode (it depends on mesh telemetry) and before Agent (so Agent's `@traced` decorators have a live event bus when the loop starts).
- No ADRs exist in this repo yet. If the team wants one, this PRD's "Architecture" section would translate naturally into ADR-0001: "Pi-alpha hosts a unified gateway for telemetry and (later) chat."

### Relationship to existing issues

- **Parent / sibling vision:** PRDs #1 and #2 (Multi-edge orchestration framework) describe the cluster this UI observes. This PRD is the operator-facing window into that fleet. It does not block, and is not blocked by, the orchestration slices.
- **Adjacent but superseded direction:** #17 ("Live-DAG Discord renderer") proposed visualising DAG state inside Discord messages. This PRD intentionally moves operator visibility off Discord and into a dedicated web UI. The two are not duplicates — #17 is DAG-scoped and Discord-bound; this is fleet-scoped and web-bound — but the design intent here is the canonical surface going forward.
