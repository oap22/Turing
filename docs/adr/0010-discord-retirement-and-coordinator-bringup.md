# ADR 0010 — Discord retirement, webui as primary operator surface, and Surface/WSL2 coordinator bringup

- Status: Proposed
- Date: 2026-05-29
- Relates to: ADR 0009 (Jetson fleet + research flywheel), CONTEXT.md
  ("Operator surfaces", "Alerts", "Vault"), PRD #228 (hardware-safety alerts).
  Supersedes the operator-surface design in `CONTEXT.md` ("Discord — the
  user-facing surface for *directing work*") and the Discord-DM closed-laptop
  alert fallback in PRD #228.

## Context

CONTEXT.md as of ADR 0009 still has Discord as the surface for *directing work*
and as the closed-laptop fallback for hardware-safety alerts. The operator has
decided to retire Discord entirely. Three forces drove the change:

1. **The webui already owns observability** (call-graph, message trace, fleet
   specs). Adding a chat pane + a question-queue manager turns it into a single
   coherent operator surface, instead of context-switching between Discord and
   the browser. The "Discord might be absorbed into a webui chat pane" line in
   CONTEXT.md becomes literal.
2. **Phase 0's actual operating model is overnight research + morning
   curation** (ADR 0009), not synchronous chat. Discord's task-thread + thumbs
   ergonomics quietly push toward real-time and away from the flywheel. A
   queue-manager surface matches the flywheel honestly; a chat pane is a
   secondary affordance for ad-hoc daytime tasks.
3. **The Surface/WSL2 coordinator has no setup script.** The most important
   node in the cluster is hand-provisioned and lives only in the operator's
   head. Discord retirement requires touching the coordinator anyway, so this
   is the moment to codify its bringup before the surface area grows further.

A Rust CLI/TUI remains deferred per ADR 0009 §5.

## Decision

### 1. Operator surfaces

- **The webui is the single primary operator surface.** It owns both fleet
  observability (existing) and work direction (new): a question-queue manager
  for the human-gated frontier (the hero), plus a chat pane for ad-hoc tasks
  (secondary, follow-on slice). Reward attribution (subtask thumb ±1.0,
  synthesis ±1.0 + fractional credit ±0.3, `critic_fallback` ±0.3) is
  preserved verbatim from the retired Discord surface; only the input
  affordance changes.
- **Discord is retired.** The task bot, surface writer, reaction listener,
  reconcile loop, cogs, and views are deleted. The alerts client survives
  temporarily — see (3).
- **Claude Code over SSH** remains the morning-review fallback when the webui
  is not in reach.

### 2. Closed-laptop alert fallback: ntfy

The Discord-DM fallback in the alerts dispatcher (PRD #228) is replaced by
**ntfy**, self-hosted on the Surface coordinator. Topic per operator
(`TURING_OPERATOR_NTFY_TOPIC`). Same trigger condition (gateway telemetry sink
has had no successful push within 90 s on an `alerting` edge); same snooze
semantics (in-memory, cleared on coordinator restart). The dispatcher swaps one
transport for another; nothing else in the alerts state machine changes.

### 3. Discord teardown ordering: half-cut

A clean cut creates a safety gap (no alert fallback until ntfy lands). A pure
strangler keeps Discord alive longer than the operator wants. **Half-cut**:

1. Stand up ntfy on the Surface.
2. Migrate the alerts dispatcher transport from Discord DM to ntfy. Delete
   `coordinator/alerts/discord_client.py`.
3. Delete the Discord task bot (`discord_bot/`, `coordinator/discord_surfaces.py`,
   reaction listener / reconcile, surface writer, views, cogs). Remove
   `discord.py` from dependencies. Remove all `TURING_DISCORD_*` env vars and
   their config-loader entries.
4. Build the webui queue manager.
5. Build the webui chat pane.

Slices 4 and 5 are not blocked by slices 1–3; they can run in parallel tracks
once ntfy is up.

### 4. Vault becomes a git repository

The vault filesystem watcher's "every commit" phrase becomes literal git
commits. The vault lives on **WSL2 ext4** (not `/mnt/c`) under the `turing`
service user, indexed via inotify (reliable; `/mnt/c` inotify is not). Mac and
any other personal device are additional git remotes. The coordinator commits
on curated promotions; reward-signal audit trail is the commit log. Obsidian
on the Surface accesses the vault via `\\wsl$\Ubuntu\home\turing\vault`.

### 5. Surface/WSL2 coordinator bringup scripts

Two scripts, mirroring the Jetson pattern:

- **`scripts/bootstrap-surface.ps1`** (PowerShell, Windows side). Scope:
  enable WSL2 + virtual machine platform features, install Ubuntu distro,
  configure `.wslconfig` with `networkingMode=mirrored`, configure `/etc/wsl.conf`
  with `[boot] systemd=true`, set Windows power plan to never sleep on AC,
  open Windows Firewall inbound rules for NATS, gateway, and ntfy ports
  scoped to the local Tailnet subnet, and register a Task Scheduler entry that
  starts `wsl -d Ubuntu` at boot and restarts it if it exits.
- **`scripts/setup-coordinator.sh`** (bash, runs inside WSL2 Ubuntu). Scope
  mirrors `setup-jetson.sh`: phased, idempotent, refuses to run on the wrong
  host (must be WSL2). Creates the `turing` service user. Installs Tailscale
  **inside WSL2 only** (Windows host is not a Tailnet node). Installs and
  configures `nats-server` (TLS + nkey auth; LAN-only). Installs `ntfy` server.
  Generates 5 NATS nkey pairs (1 coordinator + 4 worker) at first run, writes
  the coordinator key under `/etc/turing/nats/`, configures `nats-server` with
  the worker public keys, and prints each worker's seed + the coordinator URL
  for manual paste into the matching Jetson's `.env`. Clones the repo. Reads
  secrets from a pre-staged `.env.coordinator.bootstrap` file (then `shred`s
  it). Installs three systemd units (see (6)).

### 6. Coordinator process layout: three systemd units

- `turing-coordinator.service` — agent loop, scheduler, planner, budget gate,
  alerts dispatcher.
- `turing-gateway.service` — webui static + WebSocket + chat/queue API.
- `turing-vault-watcher.service` — git/fs watcher, vector index.

Plus stock `nats-server.service`, `ntfy.service`, and `tailscaled.service`.
Each unit `Restart=always`. The coordinator unit (and only the coordinator
unit) fires a cold-start ntfy notification ("coordinator back online, last
episode at T") so reboots are observable without spamming three notifications.

### 7. Always-on posture: restart-and-report, not fight-Windows

Surface power plan is "never sleep on AC." Windows Update is **not** suppressed
— the coordinator rides through reboots cleanly via durable state + auto-start.
Autologon is not used. The Task Scheduler entry from (5) restarts WSL2 if it
exits; the systemd units restart their own processes. On cold-start the
coordinator sends one ntfy push.

### 8. Tailscale, NATS, ntfy locations

- **Tailscale**: inside WSL2 only. Mirrored networking carries Tailnet
  reachability to/from the Jetsons. Windows host is not a Tailnet node.
- **NATS**: inside WSL2, next to the coordinator.
- **ntfy**: inside WSL2, separate systemd unit.

### 9. Worker `.env` additions

`setup-jetson.sh` Phase 4 grows two required fields:

- `TURING_NATS_URL` — coordinator NATS URL over Tailnet.
- `TURING_NATS_NKEY_SEED` — worker's seed, pasted from coordinator first-run
  output.

The worker systemd unit refuses to start if either is empty.

### 10. Retired

- `scripts/setup_pi.sh` — deleted as part of this change (Pis already retired
  in ADR 0009).

## Consequences

- The webui becomes load-bearing for daily operation, not just diagnostics.
  Downtime of `turing-gateway.service` now means the operator cannot direct
  new work (existing in-flight tasks continue under the agent loop). Acceptable
  because (a) the gateway is the simplest of the three units and (b) the
  morning-review flow uses Claude Code over SSH, not the webui.
- The reward-signal pipeline must follow the surface migration: every reward
  source (subtask thumb, synthesis thumb, critic_fallback) needs a webui
  emitter before the Discord bot can be deleted. Slice 3 (delete bot) is
  gated on slices 4–5 being functional for reward capture, not just UI.
  Practically: ship a reward-emitter stub in the queue manager slice before
  deleting the Discord reward emitter.
- WSL2 mirrored networking is a relatively new Windows feature. If the
  operator's Windows build doesn't support it cleanly, the script must fall
  back to NAT mode and a port-forwarding table — to be discovered during
  slice 1, not pre-engineered.
- The vault becoming a git repo means external Obsidian Sync of the same vault
  is incompatible (two writers, same files). The operator is on board with
  this trade.
- 5 hand-pasted NATS nkey seeds is an operational pain point at fleet sizes
  >5. Phase 1+ will likely move to self-service enrollment over a one-shot
  bootstrap endpoint; out of scope here.

## References

- ADR 0009 (Jetson fleet + research flywheel) — the surface and bringup
  decisions here are downstream of the operating model defined there.
- PRD #228 (hardware-safety alerts) — alert dispatcher transport swap.
- CONTEXT.md "Operator surfaces", "Alerts", "Vault" — updated inline.
