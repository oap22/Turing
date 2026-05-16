# ADR 0008 — Mesh peer discovery: replace Pyre/Zyre with NATS presence subjects

- Status: Proposed
- Date: 2026-05-16
- Relates to: Issue #198 (Pyre dependency unspecified and not on PyPI). Touches `src/turing/mesh/discovery.py`, `src/turing/mesh/node.py`, and the existing `src/turing/transport/nats_bus.py` already used by the close-the-loop dispatch pipeline.

## Context

`turing.mesh.discovery.PeerDiscovery` imports `pyre.Pyre` to run Zyre UDP-broadcast peer discovery on port 5670. In practice this has never worked in the local fleet simulation or on a real Pi, for three compounding reasons:

1. **No usable PyPI distribution.** `pyre` on PyPI is an unrelated Caltech
   scientific framework. `pyre2` is a Google RE2 wrapper. The actual Zyre
   Python binding lives at `github.com/zeromq/pyre` and is not published
   to PyPI under any obvious name. The runtime warning even points at
   the wrong package ("Install pyre2 to enable mesh networking"), so
   every operator who has tried to fix this has installed the wrong
   thing.

2. **No declared dependency.** `pyproject.toml` does not list any
   `pyre`/Zyre binding, the runtime `Dockerfile` does not install one,
   and `mypy` is configured with `module = ["pyre", ...]` set to ignore
   missing imports — masking the gap. The `try/except ImportError` in
   `discovery.py` falls through to a no-op "degraded" mode, so the
   failure is silent: each node logs `pyre_not_available — Peer
   discovery disabled` and runs as a singleton.

3. **The transport stack already standardised on NATS.** The
   close-the-loop pipeline (`src/turing/transport/nats_bus.py`,
   `transport/discovery.py`, dispatch client, canary gate runner, eval
   workers) all speak NATS. `nats-py>=2.6` is a declared dependency.
   Pyre would be a second message bus running in parallel to NATS, with
   its own UDP-broadcast network requirements (which docker-compose
   bridge networking does not pass through cleanly without extra
   configuration), purely to do peer presence.

Net effect today: the multi-node graph in the operator UI shows only
pi-alpha's solo agent loop. There is no mesh. The implementation in
`mesh/discovery.py` is dead code that pretends to work.

This ADR decides the transport and pin strategy. Implementation is a
follow-up issue.

## Options surveyed

### (a) Keep Pyre, pin to a known-good commit

Vendor or `pip install git+https://github.com/zeromq/pyre@<sha>` in
`pyproject.toml` and `Dockerfile`. Fix the misleading warning copy.

- **Pro.** Zyre is a mature, purpose-built peer-presence protocol with
  SHOUT/WHISPER group semantics, automatic ENTER/EXIT, header-based
  capability advertisement — exactly what `discovery.py` was designed
  against. Minimal code change once the dep installs.
- **Con.** Pulls in libzmq + libczmq + libzyre native deps in the Pi
  image. UDP beacon (port 5670) requires docker-compose network config
  changes to discover across the bridge; native-host Pi deployment is
  fine but the dev/test simulation that operators actually use stays
  fragile.
- **Con.** Two message buses in the stack — NATS for dispatch, Zyre for
  presence — with non-overlapping failure modes. Twice the operator
  surface area for an agent fleet that runs at single-digit node count.
- **Con.** Unpinned `master` is unsafe (the issue makes this explicit);
  pinning to a SHA means we own a security-patch responsibility on a
  binding that is not actively released.

### (b) Replace Pyre with NATS presence subjects

Move presence onto the NATS bus we already operate. Two subjects:

- `mesh.presence.heartbeat` — JSON payload
  `{node_id, node_name, capabilities, ts_ms}`, published every 10s by
  each node.
- `mesh.presence.leave` — published on graceful shutdown with
  `{node_id, ts_ms}`.

Each node subscribes to both subjects. The `MeshNode.add_peer` /
`remove_peer` / `prune_stale_peers` machinery already handles the
state transitions; the discovery layer becomes a thin NATS
publisher/subscriber.

- **Pro.** Removes a third-party dep with no PyPI story. Removes the
  `pyre` mypy override and the misleading log line in one motion.
- **Pro.** Uses transport we already operate, harden, monitor, and
  have integration tests against (`tests/test_transport/`).
- **Pro.** docker-compose simulation Just Works — NATS is already the
  inter-node channel and is bridge-network-friendly.
- **Pro.** The operator-UI agent graph (per issue context) can
  subscribe to the same `mesh.presence.*` subjects instead of scraping
  per-node state — one source of truth.
- **Pro.** Capability advertisement is a JSON payload, not a Zyre
  header dict — easier to evolve (versioning, schema).
- **Con.** We lose Zyre's SHOUT (group multicast) and WHISPER
  (peer-to-peer point-to-point) semantics as a primitive. In practice
  the `discovery._handle_event` SHOUT/WHISPER branch is a `logger.debug`
  stub — nothing in the codebase consumes those events today.
  `MeshClient.broadcast` and `MeshClient.send_task` are the application
  primitives, and they already publish through an `_outbox` queue that
  a transport layer drains. NATS subject-based pub/sub (`mesh.broadcast`,
  `mesh.direct.<node_id>`) covers both shapes without ceremony.
- **Con.** Heartbeat traffic is N² (every node publishes, every node
  subscribes). At ≤8 nodes, ≤1 msg/node/10s, this is noise. Above 50
  nodes it would need a redesign; that ceiling is well above current
  and planned cluster size.
- **Con.** Presence depends on the same bus as dispatch — a NATS
  outage takes both down simultaneously rather than degrading
  independently. Acceptable: a NATS outage already takes the
  close-the-loop pipeline offline; the agent fleet is non-functional
  in that state regardless of presence.

### (c) mDNS / Avahi service registry

Standard service-discovery via mDNS records. `python-zeroconf` is on
PyPI and maintained.

- **Pro.** No bus dependency; works on a flat LAN without coordinator.
- **Con.** mDNS in docker-compose has the same bridge-network friction
  Zyre has. Real Pi LAN is fine; the dev simulation is not.
- **Con.** Still two transports (mDNS for presence, NATS for messages),
  same operator-surface objection as (a).

### (d) Static config file listing peers

Each node reads `peers.yml` at boot.

- **Pro.** Zero infrastructure.
- **Con.** Defeats the point of "mesh." Requires every node to know
  every other node ahead of time. Doesn't survive node addition without
  config push. Not a serious option for autonomous Pi fleet.

## Decision

Adopt **option (b): NATS presence subjects**.

Reasoning:

- The supply-chain problem in the issue (no PyPI dist, three unrelated
  packages named `pyre*`, no security-patch story) is structural for
  Pyre/Zyre Python bindings. Option (a) accepts a recurring maintenance
  cost we don't need to take.
- NATS is already a load-bearing runtime dependency. Adding presence
  to it adds no new operator surface, no new ports, no new failure
  modes that weren't already present.
- The SHOUT/WHISPER loss is theoretical — the codebase does not use
  those event types. Application-level `MeshClient.broadcast` and
  `send_task` map cleanly onto NATS subject conventions.
- The operator-UI mesh graph (called out in CLAUDE.md's "Multi-Node
  Architecture" section and in the issue as a downstream symptom) can
  subscribe to `mesh.presence.heartbeat` directly and render the graph
  from a single canonical stream rather than scraping per-node `peers`
  dicts.
- Docker-compose simulation — the dev loop operators actually run —
  works immediately. Pyre's UDP beacon does not, without additional
  network configuration the project has never documented.

## Consequences

### What changes

- New module: `src/turing/mesh/presence.py` (NATS-backed
  `PeerDiscovery` replacement). Same public shape as today —
  `start()`, `stop()`, `is_running`, drives `MeshNode.add_peer` /
  `remove_peer`.
- Two new subjects on the existing NATS bus:
  - `mesh.presence.heartbeat` — periodic, 10s cadence, payload
    `{node_id, node_name, capabilities, ts_ms, schema_version: 1}`.
  - `mesh.presence.leave` — one-shot on graceful shutdown.
- `MeshNode.prune_stale_peers` becomes the liveness mechanism (already
  exists, 60s stale threshold). A missed `leave` falls back to the
  existing prune sweep — no new code path for ungraceful exit.
- `mesh/discovery.py` is deleted along with the `try/except ImportError`
  fallback. `pyproject.toml` `mypy` block loses the `"pyre"` override.
- The misleading `"Install pyre2 to enable mesh networking"` log line
  goes away with the file.
- `CLAUDE.md` "Mesh discovery uses Pyre (Zyre UDP broadcast) on port
  5670" updates to "NATS presence subjects on the shared bus." Docker
  Compose loses the unused port 5670 publication.

### What we lose

- **Pyre SHOUT/WHISPER as a transport primitive.** Currently unused
  (the handler is a debug log). Application messaging goes through
  `MeshClient` which already abstracts the transport — no caller is
  affected.
- **Independent presence-vs-dispatch failure modes.** A NATS outage
  takes both. Acceptable: a NATS outage already disables the agent
  pipeline; presence not surviving it is not the operator's first
  problem.
- **Sub-10s peer discovery.** Zyre's UDP beacon discovers in under a
  second; NATS heartbeat at 10s cadence is detect-within-10s. Mesh
  peer churn is rare (Pi restart, deploy); the latency does not affect
  request routing because `MeshClient.send_task` resolves against the
  current `peers` dict at send time.

### What we gain

- One less unmaintained, unpublished dependency.
- One less native-library surface in the Docker image.
- A presence stream the operator-UI mesh graph can subscribe to as
  the canonical source.
- The Pi fleet simulation works the first time a new operator clones
  the repo (the issue's original symptom).

### Migration sketch

A follow-up issue, scoped to:

1. Implement `src/turing/mesh/presence.py` using the existing
   `transport/nats_bus.py` client. TDD against in-memory NATS stub
   the way `tests/test_transport/test_nats_bus.py` already does.
2. Switch `__main__.py` step "MeshNode + PeerDiscovery (if
   mesh_enabled)" to construct the new presence module.
3. Delete `src/turing/mesh/discovery.py` and its tests.
4. Remove `"pyre"` from the `mypy` override list in `pyproject.toml`.
5. Remove port 5670 publication and the Pyre-related comments from
   `docker-compose.yml`.
6. Update `CLAUDE.md` Multi-Node Architecture paragraph.
7. Operator-UI subscription to `mesh.presence.*` is its own
   follow-up — out of scope for the discovery migration.

The migration is a single PR; there is no installed-base of working
Pyre nodes to roll forward from.

## Out of scope

- The application-message transport (`MeshClient` outbox →
  wire-format mapping). That migration was already underway via
  `transport/nats_bus.py` for dispatch; reusing the same bus for
  application mesh messages is a separate, larger refactor.
- Authenticated presence (signed heartbeats). Capability-token v1
  (ADR 0003) covers the dispatch side; presence is read-only-public
  for v1 here.
- Operator-UI graph implementation against the new presence stream.
- Hardening for >50-node clusters. Current and planned scale is
  single-digit nodes; revisit if/when scale changes.
