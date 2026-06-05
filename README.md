# Turing
Hybrid agentic orchestration and self improvement framework for research applications

## Local fleet

Bring up the 4-node Docker Compose simulation (Ollama + NATS + 4 turing nodes):

```bash
./scripts/dev/fleet-up.sh    # requires a populated .env
./scripts/dev/fleet-down.sh  # stop (preserves volumes; pass -v on `docker compose down` to nuke)
```

`fleet-up.sh` tails pi-alpha for ~10s so first-boot errors surface, then
prints the gateway URL (default `http://localhost:8765/`).

## Entry points

| Command | Purpose |
|---------|---------|
| `turing` | Run a node (coordinator or worker, per `.env`). |
| `turing-gateway` | Standalone gateway process — webui static + WebSocket + chat/queue API (ADR 0010 §6). Opt-in only; see below. |
| `turing-ui` | Resolve + token-handoff and open the webui in the browser. |
| `turing-vault-watcher` | Daemon that reindexes the operator's Obsidian vault by diffing each new git commit. |
| `turing-tui` | Rust/ratatui terminal operator surface (built from `tui/`; see `tui/README.md`). |

## Operator surfaces

The **webui** (served by the gateway) is the primary operator surface since the
Discord retirement (ADR 0010). It is **interactive**, not read-only — it owns
both fleet observability and work direction:

- **Question-queue manager** — the human-gated research frontier
  (`proposed → approved → in-flight → drafted → curated`), the hero pane.
- **Chat pane** — ad-hoc task submission.
- **Fleet specs** — per-node CPU/mem/disk/temp/uptime with hardware-risk
  colour-shifting; stale peers dim.
- **Message trace** — recent inter-node messages and intra-node LLM/tool calls
  with redacted, truncated prompt samples.
- **Alerts** — a persistent top banner driven by `alert` WebSocket frames, with
  **ntfy** push as the closed-laptop fallback.

The **TUI** (`turing-tui`) is the keyboard-first counterpart — a standalone Rust
client of the *same* gateway HTTP/WS API, mirroring the queue / chat / specs /
trace / alert panes with byte-identical curation reward semantics. Chosen for
latency (native render loop, idle CPU ≈ 0). See `tui/README.md`.

Open either over the Tailnet:

```bash
pip install -e .
export TURING_GATEWAY_TOKEN=…                 # the random string in the coordinator's .env
# webui:
export TURING_GATEWAY_HOST=100.x.y.z          # coordinator's Tailscale IP
turing-ui                                     # opens the browser
# TUI:
export TURING_GATEWAY_URL=http://100.x.y.z:8765
./tui/target/release/turing-tui
```

`turing-ui` validates the token via `/healthz`, then hands it off through
`/token-handoff?token=…` so the token never sits in the address bar after the
first hop. The session lives in an http-only cookie until the browser tab
closes. See `docs/operator/connectivity.md` for the full Tailnet setup.

**Where the gateway runs.** A deployed coordinator runs the gateway
**in-process** (`TURING_GATEWAY_ENABLED`, default `false`; the coordinator
flips it on). The standalone `turing-gateway` unit is opt-in via
`TURING_GATEWAY_STANDALONE` and exists for the future three-unit coordinator
split — until an IPC channel lands it serves *empty* projections, so it is not
the default path. (See `src/turing/gateway/__main__.py`.)

### Preview locally with seeded data

`scripts/dev/demo_gateway.py` boots the real gateway wiring and seeds every pane
(queue across all five columns, a chat thread, a Surface + 4 Jetson specs panel
with a hot/danger temp, a trace backlog, a firing alert) so you can drive the
webui **and** TUI without a live fleet:

```bash
TURING_GATEWAY_TOKEN=demo .venv/bin/python scripts/dev/demo_gateway.py --port 8765
# webui: http://127.0.0.1:8765/token-handoff?token=demo
# TUI:   TURING_GATEWAY_URL=http://127.0.0.1:8765 TURING_GATEWAY_TOKEN=demo \
#        ./tui/target/release/turing-tui
```

State is in-memory and resets on restart.

## Database migrations

Schema lives under `alembic/versions/` as raw-SQL migrations authored with
`alembic.op.create_table`/`op.execute` — the project does **not** define
SQLAlchemy declarative models, so `alembic/env.py` exposes
`target_metadata = None`.

Supported commands:

```bash
alembic upgrade head    # apply pending migrations
alembic current         # show current revision
alembic history         # list revisions
```

**`alembic check` is not supported.** Without a `MetaData` object it cannot
diff models against the DB; running it errors with "environment script
alembic/env.py does not provide a MetaData object". Drift is caught by
hand-written migration tests under `tests/test_alembic_*.py` instead.
