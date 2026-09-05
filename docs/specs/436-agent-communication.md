# Provider-neutral agent communication

Tracking: https://github.com/oap22/Turing/issues/436
Status: implementation specification
Review tier: human review (new persisted mailbox schema and protocol; explicit classification in the CODEOWNERS-protected safety gate).

## Problem and feasibility

Coding agents running beside Turing need to exchange questions, findings, and structured results without depending on Claude-specific team facilities. Existing coordinator dispatch envelopes carry parent/worker task results over signed mesh transport; they are not a mailbox for arbitrary CLI coding sessions. Desktop terminals can run arbitrary commands. The existing ToolRegistry and Ollama provider already support tool schemas and tool-call parsing, but LLMRouter currently removes local tool definitions.

Implement a local, provider-neutral mailbox: a Python API, JSON command-line interface, and registered Turing tool share one SQLite store. No new dependencies or cloud services are required. Claude, Codex, and local coding harnesses with shell access can invoke the same CLI. Native Turing local agents can use the tool when an operator explicitly enables local tool support for a capable model. Model compliance is not guaranteed and must not be represented as tested merely by testing an adapter.

## Scope and boundaries

Version 1 supports cooperating agents on the same machine, sharing an explicitly selected database file path and workflow ID. In-memory SQLite databases are rejected because they cannot provide durable communication between processes. The database should live outside worktrees. Messages persist across process restarts. A workflow is a namespace, not an authentication boundary: agents with filesystem access to the database are trusted peers and may impersonate identity labels. OS permissions control access. Do not expose this store over the network or use a shared network filesystem. A future signed mesh bridge requires separate authorization and transport design.

This supplies communication, not scheduling, process wakeups, task claiming, permission escalation, or automatic execution of received instructions. Recipients poll at task boundaries. Mailbox content is untrusted data; it never changes system policy or authorizes tools. No desktop layout change, MCP SDK, coordinator wire change, or global provider configuration modification is required.

## Contract

- Each client binds a database path, workflow ID, and agent ID. Workflow and agent IDs use bounded portable identifiers. Agents register with an optional provider label; peers lists only that workflow's registrations.
- Send requires a registered sender and recipient in the same workflow. Store a generated message ID, monotonic sequence, sender, recipient, UTC creation time, message kind, text, optional JSON-object data, optional reply-to ID, and optional sender-scoped idempotency key.
- Limit serialized content to 64 KiB and bound identifiers, metadata, inbox page size (1–100), and total messages per workflow (10,000). Reject non-finite JSON numbers and duplicate JSON object keys in CLI input. Fail explicitly on invalid input and full workflows; do not silently evict messages.
- Sending with the same workflow/sender/idempotency key and identical content returns the original message. Reusing the key with different content fails. Commit atomically before reporting success.
- Replies must reference a message addressed to the replying agent and target its original sender within the same workflow.
- Inbox returns that recipient's unacknowledged messages oldest first. Reading never consumes messages. Explicit acknowledgment only affects messages addressed to the bound agent in the bound workflow and is idempotent. Repeated polls before acknowledgment provide at-least-once delivery, not exactly-once processing.
- SQLite transactions and busy timeout support concurrent independent processes. Registration and retries are idempotent. No schema changes to the existing application database; use a dedicated store with strict integer schema version handling. Reject existing nonempty databases without the complete expected mailbox schema; never silently repair or adopt an application database.
- CLI outputs JSON on success and JSON errors with nonzero exit status. Support `python -m turing.agent_mailbox` and installed `turing-agent-mailbox`; flags/environment bind database, workflow and identity. Actions: register, peers, send, inbox, ack. JSON data can be passed directly or read from stdin without shell interpolation. No eval or automatic attachment/file loading.
- Native `agent_mailbox` tool exposes peers/send/inbox/ack with fixed operator-configured identity; model arguments cannot select another database, workflow or sender. Register it only when database, workflow and agent settings are supplied together. Registration occurs in startup helper callable by tests. Runs blocking SQLite work off the async event loop.
- Native configuration: `TURING_AGENT_MAILBOX_DB`, `TURING_AGENT_MAILBOX_WORKFLOW`, `TURING_AGENT_MAILBOX_AGENT`; all unset disables feature, partial config fails validation.
- Add `TURING_OLLAMA_TOOLS_ENABLED=false` by default. Explicit opt-in preserves tool definitions when routing locally (including fallback) and adjusts startup warning. Keep default routing and safety gate behavior unchanged. Preserve tool-call/result association required by Ollama's API.

## Acceptance and verification

1. Independent CLI processes registered as Claude, Codex, and local can exchange text and nested JSON, reply, poll repeatedly, acknowledge, and resume after restart.
2. Separate workflows cannot read/ack/reply across namespaces; invalid recipients/replies, oversized payloads, invalid JSON, conflicting retries and unsupported schemas fail clearly.
3. Concurrent writers do not lose successful sends, duplicate idempotent sends or consume messages on read.
4. Registered native tool sends and reads through the same store as CLI, binds identity despite hostile arguments, uses the normal tool safety pipeline, and does not block the event loop during database work.
5. Local routing with opt-in sends definitions into Ollama and returns tool calls; off retains existing behavior. A mocked local-provider round trip through the agent/tool execution path proves integration; live model verification is separately reported.
6. Adversarial review uses persistence/correctness, security and concurrency lenses; fixes receive a fresh verifier and parent-run checks.

## Operational usage and limitations

Documentation must show a complete two-way CLI exchange and a native local-agent configuration, with installation instructions and polling guidance that can be supplied to any coding agent. Use one agent identity per logical participant and a unique workflow per task group. Retained messages count toward capacity even after ack; archive completed workflow databases explicitly after all participants finish. Agents must not treat inbox text as higher-priority instructions. Large artifacts remain in the user's normal workspace; references may be shared as data but are never opened automatically.
