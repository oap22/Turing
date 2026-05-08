# Turing
Local Agentic assistant for Edge AI applications

## Operator UI

Pi-alpha hosts an in-process gateway that serves a read-only observability
SPA at a configurable Tailscale-only bind. To open it from your laptop:

```bash
pip install -e .
export TURING_GATEWAY_HOST=100.x.y.z   # pi-alpha's Tailscale IP
export TURING_GATEWAY_TOKEN=…           # the random string set in pi-alpha's .env
turing-ui                              # opens the browser
```

`turing-ui` resolves the host, validates the token via `/healthz`, and hands
it off to the browser through `/token-handoff?token=…` so the token never
sits in the address bar after the first hop. The session lives in an
http-only cookie until the browser tab closes.

The SPA shows three panes:

- **Layered call-graph** — Pi-nodes on the left, their tools and memory
  stores in the middle, LLM providers on the right. Edges pulse with live
  latency as calls fire (slice 6).
- **Message trace** — recent inter-node messages and intra-node LLM/tool
  calls with redacted, truncated prompt samples (slice 7).
- **Debug stream** — the raw frame log used during slice 5 bring-up; useful
  when something looks off in the upper panes.

The pi-alpha node is the only one that runs the gateway —
`TURING_GATEWAY_ENABLED` defaults to `false`, and only pi-alpha's compose
file flips it on.
