# turing-tui

A snappy terminal operator surface for the Turing cluster — the keyboard-first
counterpart to the webui. It speaks the **same gateway HTTP/WS API**, so it
mirrors every operator surface (question queue, chat, fleet specs, message
trace, hardware alerts) without any new backend.

Built in Rust (ratatui + crossterm + tokio) for latency: a native render loop,
event-driven redraws (idle CPU ≈ 0 — it only repaints when state actually
changes), and all socket I/O off the render path. Honours ADR 0009 §5's
"narrow Rust use for the CLI/TUI; orchestration stays in Python".

## Theme

Both operator surfaces share one "minimalist computer" vocabulary
(see `src/ui/theme.rs` here and `webui/src/index.css`):

- near-black canvas, dim grey chrome, a single **cyan** accent;
- green / yellow / red reserved for ok / warn / danger semantics
  (magenta marks work awaiting a human decision);
- block-element gauges (`▮▮▮▯▯`) instead of decorative widgets;
- sharp 1-px borders, lowercase pane tags, keycap-style footer hints.

## Build

```bash
cd tui
cargo build --release      # → target/release/turing-tui  (~3 MB, stripped)
```

Requires a Rust toolchain (1.82+). No Node, no Python — it is a standalone
client of the gateway.

## Run

```bash
# point it at the gateway over the Tailnet, with the bearer token
turing-tui --url http://surface.<tailnet>.ts.net:8765 --token "$TURING_GATEWAY_TOKEN"

# or via environment (matches the gateway's own contract)
export TURING_GATEWAY_URL=http://surface.<tailnet>.ts.net:8765
export TURING_GATEWAY_TOKEN=...      # = coordinator .env.coordinator TURING_GATEWAY_TOKEN
turing-tui
```

The token is the coordinator's `TURING_GATEWAY_TOKEN`. `https://` URLs are
upgraded to `wss://` automatically. `turing-tui --check` prints the resolved
config and exits (handy in CI / smoke tests).

## Panes & keys

| Pane | Key | Actions |
|------|-----|---------|
| **Queue** | `1` | `h`/`l` move column, `j`/`k` row · `a` approve (proposed) · `y` accept / `n` reject / `e` edit (drafted) |
| **Chat** | `2` | `j`/`k` thread, `h`/`l` subtask · `i` new prompt · `y`/`n`/`e` thumb a completed subtask |
| **Specs** | `3` | `j`/`k` rows — temp/disk colour-shift toward danger; stale peers dim |
| **Trace** | `4` | `j`/`k` scroll · `f` toggle follow (auto-scroll to newest) |
| **Alerts** | `5` | `j`/`k` rows · `s` snooze the selected `(peer, field)` for 4h |
| **Help** | `?` | keybinding reference |

Global: `Tab`/`Shift-Tab` cycle panes, `q` or `Ctrl-C` quit. In an input box:
type, `Enter` submits, `Esc` cancels.

Curation reward semantics are identical to the webui (accept +1.0, reject −1.0,
edit +0.3) because both surfaces hit the same gateway endpoints — the input
affordance changes, the reward rows do not.

The top banner mirrors the webui's persistent hardware-alert banner; the
connection indicator (`● live` / `◌ connecting` / `● offline`) reflects the WS,
which auto-reconnects with capped backoff through coordinator restarts.

## Architecture

```
main.rs   terminal + tokio runtime; the select! render loop (redraw on dirty only)
cli.rs    clap config (url/token/poll), http→ws derivation
model.rs  wire types + tolerant frame parser (mirrors the gateway's JSON exactly)
api.rs    reqwest client for operator actions + /peers, /api/events pulls
ws.rs     self-healing WebSocket subscription (bearer upgrade, backoff)
event.rs  EngineEvent channel between the I/O tasks and the loop
app.rs    all state + PURE reducers (apply_frame / on_key → Vec<Action>)
ui/       ratatui rendering: mod.rs (layout), panes.rs, theme.rs
```

`app.rs` is intentionally pure — every state transition is a function of decoded
input with no I/O — so the whole surface is unit-tested without a live gateway
(`cargo test`: frame parsing, reducers, key→action mapping, and a `TestBackend`
render smoke test that draws every pane, the modal, and a 1×1 terminal without
panicking).

## Test / lint

```bash
cargo test
cargo clippy --all-targets -- -D warnings
cargo fmt --check
```
