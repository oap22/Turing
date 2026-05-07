# turing-webui

Vite + React + Tailwind + React Flow scaffold for the fleet-observability SPA
served by the pi-alpha gateway.

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
