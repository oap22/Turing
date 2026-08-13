# Turing

Research tool for the new age of development — an autonomous research agent with a
keyboard-first desktop operator surface.

Turing is two things that meet in one repo:

1. **A research agent** (`src/turing/research/`, ADR 0011): given a
   `(goal, verifier)` pair it iterates until the frozen verifier passes or a cap
   trips. The verifier is frozen — the agent invents, the operator holds the
   ruler. It cannot quit; it escalates with a three-word vocabulary
   (`continue / abandon / extend_cap`). Research discipline lives in the
   operator's `/research-interview` → `/research-loop` workflows; agreed briefs
   sit in `research/briefs/`, and its own results default to `~/research-results/`
   — the same root the desktop below watches, so a loop run from any project
   renders live (`TURING_RESEARCH_RESULTS_ROOT` repoints it).
2. **Turing Desktop** (`desktop/` + `webui/src/desktop/`, issue #382): a Tauri 2
   macOS app hosting the webui SPA as a Hyprland/omarchy-style tiling shell —
   real PTY terminals for coding-agent CLIs and vim, live training charts, a
   flywheel round timeline, an image viewer, and a viewer for Claude/Codex agent
   sessions. Everything it renders is a plain file under the watched results
   root (`~/research-results/` by default, any project may write there), so
   the same data is readable (and writable) by coding agents.

The earlier fleet stack — coordinator, NATS mesh, Jetson workers, webui
queue/chat, LoRA trainer (ADRs 0001–0010) — is **dormant in-tree, not deleted**:
loop 1 runs Mac-local with heavy jobs dispatched to an ssh cluster. The chat
pane is reserved for the future nano agent network.

## Turing Desktop

```bash
cd webui && npm ci --legacy-peer-deps      # once
cd ../desktop && npm ci && npm run dev     # opens the app (tauri dev)

npm --prefix desktop run install-app       # build + (re)install /Applications/turing.app
```

- **mod = ⌘**, omarchy semantics: ⌘Return terminal · ⌘W close · ⌘hjkl/arrows
  focus (cursor warps with you) · ⌘⇧ move · ⌘1–5 workspaces · ⌘F zoom ·
  ⌘P launcher · ⌘/ cheatsheet. Layout persists.
- **Data contract**: append `~/research-results/<run>/metrics.jsonl`
  (`{"step": n, "total_steps": N, "ts": t, ...numeric series}`) and charts move
  live; `loop-<slug>/trajectory.json` renders the flywheel timeline; SVG/PNG
  plots land in the images pane. Agents steer the view via
  `~/research-results/.viewer.json`. Remote (ssh) runs stream in through the
  `ssh-follow-metrics` / `ssh-pull-assets` runners.
- Config (gateway URL/token, filesystem roots): `~/.config/turing-desktop/config.json`.
- Updating the installed app: `npm --prefix desktop run install-app`
  (`scripts/install-desktop.sh` — build, quit, replace, re-sign, report version).
- Full keymap, panes, runners, troubleshooting: `desktop/README.md`.

## Dormant fleet stack

<details>
<summary>Multi-node simulation, gateway, browser webui, TUI (ADR 0009/0010 era)</summary>

### Local fleet

```bash
./scripts/dev/fleet-up.sh    # 4-node Docker Compose sim (Ollama + NATS); needs .env
./scripts/dev/fleet-down.sh
```

### Entry points

| Command | Purpose |
|---------|---------|
| `turing` | Run a node (coordinator or worker, per `.env`). |
| `turing-gateway` | Standalone gateway — webui static + WS + chat/queue API (ADR 0010 §6). Opt-in. |
| `turing-ui` | Resolve + token-handoff and open the webui in the browser. |
| `turing-vault-watcher` | Reindex the operator's Obsidian vault per git commit. |
| `turing-tui` | Rust/ratatui terminal operator surface (`tui/`). |

### Browser webui / TUI

The browser webui (queue manager, chat, fleet specs, message trace, alerts) is
unchanged by the desktop shell — the SPA renders the tab UI outside Tauri. The
TUI is a standalone Rust client of the same gateway API. Connect over the
Tailnet:

```bash
pip install -e .
export TURING_GATEWAY_TOKEN=…                 # coordinator's .env token
export TURING_GATEWAY_HOST=100.x.y.z          # webui: coordinator's Tailscale IP
turing-ui
export TURING_GATEWAY_URL=http://100.x.y.z:8765   # TUI
./tui/target/release/turing-tui
```

`turing-ui` validates the token via `/healthz`, then hands it off through
`/token-handoff?token=…`; the session lives in an http-only cookie. See
`docs/operator/connectivity.md`.

A deployed coordinator runs the gateway **in-process**
(`TURING_GATEWAY_ENABLED`, default `false`). Standalone `turing-gateway`
(`TURING_GATEWAY_STANDALONE`) serves empty projections until an IPC channel
lands.

### Preview with seeded data

```bash
TURING_GATEWAY_TOKEN=demo .venv/bin/python scripts/dev/demo_gateway.py --port 8765
# webui: http://127.0.0.1:8765/token-handoff?token=demo
# TUI:   TURING_GATEWAY_URL=http://127.0.0.1:8765 TURING_GATEWAY_TOKEN=demo \
#        ./tui/target/release/turing-tui
```

</details>

## Database migrations

Raw-SQL alembic migrations under `alembic/versions/` (no declarative models;
`target_metadata = None`):

```bash
alembic upgrade head    # apply
alembic current         # show
alembic history         # list
```

**`alembic check` is not supported** (no `MetaData` to diff). Drift is caught by
`tests/test_alembic_*.py`.
