# ADR 0010 — Implementation slices (swarm-ready)

Each slice is independently grabbable by an agent. Dependencies are explicit.
Hand this file to `/to-issues` to mint GitHub issues, or fan it out directly to
parallel coding agents.

## Slice A — Stand up ntfy on the Surface coordinator
**Deps:** none. **Owner:** infra. **Blocks:** B.

- Install `ntfy` server inside WSL2 on the Surface as a systemd unit
  (`ntfy.service`, `Restart=always`).
- Topic per operator. Add `TURING_OPERATOR_NTFY_TOPIC` to config + `.env.example`.
- Open Windows Firewall inbound for ntfy's port from the local Tailnet subnet
  only (manual PowerShell snippet OK; will be folded into bootstrap script in F).
- Verify with `curl -d "test" https://ntfy.surface.<tailnet>/<topic>` from a
  Jetson, confirm push lands on operator's phone.

## Slice B — Swap alerts dispatcher Discord → ntfy
**Deps:** A. **Owner:** product.

- Add `coordinator/alerts/ntfy_client.py` (HTTP POST to topic; tiny).
- Update `coordinator/alerts/dispatcher.py`: replace the Discord-DM fallback
  branch with ntfy. Same 90 s sink-stale trigger, same `(peer, field)` snooze
  semantics, same in-memory state.
- Delete `coordinator/alerts/discord_client.py`.
- Update PRD #228 references and the "Alerts" section of CONTEXT.md if
  anything is now stale (the alerts paragraph was already updated in this ADR's
  prep).
- Tests: dispatcher unit tests that previously asserted Discord DM now assert
  ntfy POST. Add a snooze-survives-ntfy-failure test.

## Slice C — Webui queue manager (the hero)
**Deps:** none (runs against current coordinator). **Owner:** product. **Blocks:** E.

- New webui pane `webui/src/queue/` rendering the human-gated question frontier
  with columns `proposed → approved → in-flight → drafted → curated`.
- WebSocket frames: `queue.snapshot`, `queue.delta`. Schema in
  `webui/src/ws.ts`.
- Coordinator side: new API endpoints on `turing-gateway` for approve / reject /
  edit on a question. New table or projection if needed for queue state.
- Reward emitter: curation accept/reject/edit decisions write to
  `episode_rewards` exactly like the Discord surface did. **This is the
  load-bearing reward path.**
- No Discord coupling.

## Slice D — Delete Discord task bot
**Deps:** B (alerts cut over), C (reward path exists in webui). **Owner:** infra.

- Delete `src/turing/discord_bot/` in full.
- Delete `src/turing/coordinator/discord_surfaces.py` and
  `src/turing/coordinator/live_dag_renderer.py` Discord codepaths (keep the
  live DAG renderer if it's used by the webui; check first).
- Strip Discord references from `agent/core.py`, `agent/executor.py`,
  `agent/safety.py`, `learning/feedback.py`, `mesh/presence.py`,
  `logging.py`, `__main__.py`.
- Remove `discord.py` from `pyproject.toml`.
- Remove all `TURING_DISCORD_*` env vars from `config.py` and `.env.example`.
- Run `gitnexus_impact` before each deletion; expect HIGH risk on
  `episode_rewards.py` and `reward_attribution.py` — the goal of staging this
  after C is that the webui already emits the same rewards.

## Slice E — Webui chat pane
**Deps:** C. **Owner:** product.

- New webui pane `webui/src/chat/` for ad-hoc task submission.
- Free-form prompt → POST to gateway → coordinator plans DAG → subtasks stream
  back into the same thread → per-subtask thumbs feed `episode_rewards`.
- Reuses the reward emitter from C.

## Slice F — `scripts/bootstrap-surface.ps1` (Windows side)
**Deps:** none formally; in practice write **after** A–E so it codifies the
running coordinator instead of being designed in the abstract. **Owner:** infra.

- Enable WSL2 + virtual machine platform features (idempotent — check first).
- Install Ubuntu distro.
- Write `.wslconfig` with `networkingMode=mirrored`; write
  `/etc/wsl.conf` inside the distro with `[boot] systemd=true`.
- Set Windows power plan to never sleep on AC.
- Open Windows Firewall inbound for NATS, gateway, ntfy ports scoped to the
  Tailnet subnet.
- Register a Task Scheduler entry that runs `wsl -d Ubuntu` at boot and
  restarts if it exits.
- Idempotent: re-runs are no-ops.

## Slice G — `scripts/setup-coordinator.sh` (WSL2 side)
**Deps:** F (so it runs inside a known-good WSL2). **Owner:** infra.

- Refuse to run unless inside WSL2 Ubuntu (`/proc/sys/kernel/osrelease` contains
  `microsoft` or `WSL`).
- Phased, idempotent, mirroring `setup-jetson.sh` style and logging helpers.
- Phase 1 system: hostname, Tailscale (inside WSL2), apt packages, `uv` for
  Python 3.11, `nats-server`, `ntfy` (idempotent w/ A).
- Phase 2 user: create `turing` service user.
- Phase 3 repo + app: clone repo, venv, install Turing in editable mode,
  embedding model.
- Phase 4 secrets: read pre-staged `/home/turing/.env.coordinator.bootstrap`,
  write `/home/turing/turing/.env.coordinator` (0600, owned by turing),
  `shred` the bootstrap.
- Phase 5 NATS keys: on first run generate 5 nkey pairs (1 coordinator + 4
  worker), write coordinator key under `/etc/turing/nats/`, configure
  `nats-server` with worker public keys, print each worker's seed + the
  coordinator NATS URL.
- Phase 6 vault: clone the vault git repo to `/home/turing/vault`, configure
  the watcher.
- Phase 7 systemd: install and start `turing-coordinator`,
  `turing-gateway`, `turing-vault-watcher`.

## Slice H — Jetson `.env` grows two NATS fields
**Deps:** G (coordinator must be issuing seeds). **Owner:** infra.

- Update `scripts/setup-jetson.sh` Phase 4: add `TURING_NATS_URL=` and
  `TURING_NATS_NKEY_SEED=` to the generated `.env`; prompt for them if running
  fresh; preserve existing values on re-run.
- Update the worker systemd unit to refuse start if either is empty.
- Document the manual paste step from G's Phase 5 output.

## Slice I — Vault becomes a git repo
**Deps:** G (vault path lives in WSL2). **Owner:** infra.

- Create a private GitHub repo for the vault.
- Move the existing vault content into it.
- Add the watcher's "diff last commit, reindex changed files" implementation.
- Document the Mac-side `git clone` + `git pull` workflow.

## Slice J — Cleanup: kill the .swp file and re-runnable verification
**Deps:** none. **Owner:** infra. (trivial)

- Remove `scripts/.setup-jetson.sh.swp` from the working tree.
- Run `setup-jetson.sh` end-to-end on a known-good Jetson, confirm second run
  is a clean no-op.

---

## Suggested swarm fan-out

Three parallel agents, each owning a track:

- **Agent 1 (alerts track):** A → B → D.
- **Agent 2 (webui track):** C → E.
- **Agent 3 (infra track):** J (warmup), then F → G → H → I after A and B
  merge so the infra agent has a stable coordinator to codify.

D blocks on both B and C merging. After D, the codebase is Discord-free.
