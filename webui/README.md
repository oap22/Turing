# turing-webui

Vite + React + Tailwind + React Flow scaffold for the fleet-observability SPA
served by the pi-alpha gateway.

## Theme

The SPA shares the TUI's "minimalist computer" vocabulary. The design tokens
live in `src/index.css` as a Tailwind v4 `@theme` block (`term-bg`,
`term-panel`, `term-raised`, `term-edge`, `term-fg`, `term-dim`,
`term-accent`): near-black canvas, monospace everywhere, one cyan accent,
emerald/amber/rose reserved for semantics, sharp 1-px borders, uppercase
micro-labels, and a `term-cursor` blink for live/streaming indicators.

## Views

The console is segmented into three tabbed views (issue #358), one visible at
a time, switched by clicking the header tabs or pressing `1`/`2`/`3` (hash-
synced: `#queue`, `#chat`, `#obs`):

- **queue** (default) — the full-screen five-column human-gated frontier.
- **chat** — master-detail: thread list left, the selected thread's subtasks
  right, prompt box pinned underneath (a submit always starts a new thread).
- **observability** — call graph + fleet specs strip, with the message-trace
  column alongside (selecting a trace event highlights the matching edge).

The alert banner and the backtick debug overlay stay global. Tab badges count
items awaiting an operator decision (proposed/drafted questions, completed-
unrewarded subtasks). All reducers stay mounted in `App.tsx`, so hidden views
keep ingesting WS frames and badges stay live.

## Build

```bash
cd webui
npm install
npm run build
```

`npm run build` writes static assets to `webui/dist/`. The Python wheel build
includes that directory; the gateway serves it from `/` and `/<asset-path>`,
gated by the bearer-token middleware.

## Dev loop

```bash
npm run dev   # vite dev server with HMR; expects gateway on :8765
```

The dev server proxies `/ws` and `/token-handoff` to the local gateway.

## Token handoff

Direct browser visits use a one-shot `?token=...` query parameter handled by
the gateway's `/token-handoff` route — it sets an http-only cookie and
redirects to `/`. The CLI launcher (slice 9) opens that URL automatically.

## Desktop shell

Everything above is unchanged when this SPA runs in a plain browser. Inside
the `desktop/` Tauri 2 shell (issue #382), `isTauri()` (`"__TAURI_INTERNALS__"
in window`, `src/desktop/tauri.ts`) flips `App.tsx` over to render
`DesktopShell` instead of the tabbed UI: a keyboard-first, Hyprland/omarchy-
style tiling window manager with local PTY terminals, live metrics/flywheel/
agent panes, and the same `QueuePane`/`ChatPane`/`ObservabilityView`
components wired through a Rust-side authenticated gateway proxy so the
token never reaches the webview. All of it lives under `src/desktop/`:

- `tauri.ts` — the only module that imports `@tauri-apps/api`.
- `gateway.ts` — HTTP/WS proxy client (`gatewayFetch`, `connectTauriWS`).
- `layout.ts` — the pure binary-split tiling core; `keymap.ts` — the ⌘
  keybindings; `theme.ts` — the omarchy color themes.
- `DesktopShell.tsx`, `Launcher.tsx`, `Cheatsheet.tsx` — the shell chrome.
- `panes/` — one module per pane (`TermPane`, `MetricsPane`, `ImagesPane`,
  `FlywheelPane`, `AgentsPane`, `AgentFeedPane`) plus their pure parsing
  helpers, which is where the desktop test coverage lives.

See `desktop/README.md` for how to run it.
