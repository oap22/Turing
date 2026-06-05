# Operator connectivity — reaching the Turing fleet (macOS & Windows 11 + WSL2)

How an operator connects to the Turing cluster from their own machine — SSH to
the nodes, the web UI over the Tailnet, the NATS bus, the vault git remote, and
the one-command fleet provisioner. Two client platforms are covered side by
side: **macOS** and **Windows 11 + WSL2 Ubuntu**. Where the steps differ, each
section has an explicit *On macOS* / *On Windows + WSL2* block.

This guide is accurate to the repo as of ADR 0009 (Jetson fleet + Surface/WSL2
coordinator) and ADR 0010 (Discord retirement + coordinator bringup). It cites
the scripts and config it documents; if a default looks wrong, check the cited
source before trusting this page.

> **Two roles of "your machine".** The Surface is the always-on **coordinator**;
> your Mac or Windows laptop is an **operator workstation** — a Tailnet client
> you use to SSH in, open the web UI, and pull the vault. The operator
> workstation runs none of the fleet services itself.

---

## 1. Overview & prerequisites

### Topology recap

```
                         Tailnet (WireGuard mesh, MagicDNS)
                                      │
        ┌─────────────────────────────┼──────────────────────────────┐
        │                             │                              │
  operator laptop              Surface Pro (coordinator)        H100 / DGX
  (Mac OR Win11+WSL2)          Windows 11 + WSL2 Ubuntu         (trainer)
   • SSH client                 ┌──────────────────────────┐    • pull-only
   • browser → web UI           │ tailscaled (in WSL2)     │    • subscribes to
   • git pull (vault)           │ nats-server  tls :4222   │      coordinator NATS
   • runs setup-fleet.sh        │ ntfy server  http :8090  │    • never dials in
     ON the coordinator         │ turing-coordinator       │
                                │   └─ gateway (web UI)     │
                                │       http :8765         │
                                │ turing-vault-watcher     │
                                └────────────┬─────────────┘
                                             │ NATS over Tailnet
                  ┌──────────────┬───────────┼───────────┬──────────────┐
              jetson-1       jetson-2     jetson-3     jetson-4
              (worker)       (worker)     (worker)     (worker)
```

- **Coordinator** — Surface Pro (16 GB), Windows 11 + **WSL2 Ubuntu**. Hosts
  `nats-server`, `ntfy`, the three `turing-*` systemd units (including the web
  UI gateway), and the vault git working tree. Tailscale runs **inside WSL2**;
  the Windows host is *not* a Tailnet node (ADR 0010 §8). See
  `CONTEXT.md` → Topology and ADR 0009 §1.
- **Workers** — 4× Jetson Orin Nano Super (8 GB), `jetson-1`…`jetson-4`. Run the
  executor loop and a local Ollama model; dial the coordinator's NATS bus.
- **Trainer** — H100/DGX, pull-only. It subscribes to coordinator NATS via
  `turing-trainer`; you do not connect to it as an operator. Out of scope here.

### What you can reach, and from where

| From your laptop | Target | How |
|---|---|---|
| SSH | coordinator (WSL2 Ubuntu) | `ssh surface` |
| SSH | each worker | `ssh jetson-1` … `ssh jetson-4` |
| Browser | web UI | `http://surface.<tailnet>.ts.net:8765/` |
| `curl` | NATS port (reachability only) | `nc -vz surface.<tailnet>.ts.net 4222` |
| `git` | vault (read-only) | `git clone`/`git pull` from your private vault repo |

Everything above rides the Tailnet. The cluster exposes **no public ports**:
NATS is TLS + nkey, LAN/Tailnet-only (`CONTEXT.md` → Runtime bus); the gateway
binds loopback by default and is firewalled to the Tailnet subnet
(`scripts/bootstrap-surface.ps1` step 6).

### Prerequisites

- A **Tailscale account** (free tier is fine) and an invite to the same tailnet
  as the fleet. You will join your laptop in §2.
- SSH key(s) you can add to each node's `authorized_keys` (§3).
- For provisioning (§5) you run `scripts/setup-fleet.sh` **on the coordinator**,
  not on your laptop — so you only need SSH to the Surface for that.
- The coordinator and workers must already be brought up:
  `scripts/bootstrap-surface.ps1` + `scripts/setup-coordinator.sh` for the
  Surface, then `scripts/setup-fleet.sh` for the Jetsons (§5).

> **Naming.** Throughout, replace `<tailnet>` with your tailnet's DNS suffix
> (the bit before `.ts.net` — visible in `tailscale status --json` as
> `MagicDNSSuffix`, or in the Tailscale admin console). MagicDNS hostnames look
> like `surface.<tailnet>.ts.net` and `jetson-1.<tailnet>.ts.net`.

---

## 2. Tailscale join

Everything in this guide assumes your laptop is a member of the same tailnet as
the fleet, with **MagicDNS** enabled (Tailscale admin console → DNS → MagicDNS:
enabled). MagicDNS is what makes `surface.<tailnet>.ts.net` resolve.

### On macOS

```bash
# Install the Tailscale client (Homebrew, or the Mac App Store build).
brew install --cask tailscale

# Bring it up and authenticate in the browser it opens.
sudo tailscale up

# Confirm you can see the fleet.
tailscale status
ping -c1 surface.<tailnet>.ts.net
```

The Mac App Store / standalone GUI build works identically — `tailscale up` from
the menu-bar app. Once up, `surface`, `jetson-1`…`jetson-4` resolve by MagicDNS
from any network.

### On Windows + WSL2

This is the part with real gotchas. The coordinator runs Tailscale **inside
WSL2** (ADR 0010 §8), and to reach it cleanly your operator WSL2 must (a) use
**mirrored networking** and (b) have **systemd enabled** so `tailscaled` runs as
a service. Install the **Windows** Tailscale client *and* run Tailscale *inside*
your WSL2 Ubuntu.

> **Note — these are the same two WSL2 settings the coordinator needs.** The
> Surface's own `scripts/bootstrap-surface.ps1` writes them on the coordinator
> (`.wslconfig` networkingMode=mirrored + `/etc/wsl.conf` `[boot] systemd=true`).
> On your operator Windows box you set them yourself, below, for the same
> reasons: mirrored networking carries Tailnet reachability into WSL2, and
> systemd lets `tailscaled` run as a managed service.

**Step 1 — `.wslconfig` (Windows side): mirrored networking.** WSL2 is NAT'd by
default, which mangles Tailnet reachability from inside the distro. Mirrored
mode shares the Windows host's network namespace with WSL2. Create or edit
`C:\Users\<you>\.wslconfig`:

```ini
# C:\Users\<you>\.wslconfig
[wsl2]
networkingMode=mirrored

[experimental]
hostAddressLoopback=true
```

(This mirrors what `bootstrap-surface.ps1` step 3 writes on the coordinator.)
Mirrored networking needs Windows 11 22H2 (build 22621) or newer — see the
troubleshooting table if Jetsons/coordinator are unreachable after this.

**Step 2 — `/etc/wsl.conf` (inside the distro): enable systemd.** So
`tailscaled` (and any other service) runs as PID-1-managed systemd units:

```ini
# /etc/wsl.conf  (inside your WSL2 Ubuntu, edit as root)
[boot]
systemd=true
```

**Step 3 — apply both.** From an **elevated PowerShell** on Windows:

```powershell
wsl --shutdown
```

then restart the distro (`wsl -d Ubuntu`). Both settings only take effect after
a full `wsl --shutdown`. Verify systemd is PID 1:

```bash
# inside WSL2
systemctl is-system-running     # "running" or "degraded" — not "offline"
```

**Step 4 — install the Windows client.** Install Tailscale for Windows from
<https://tailscale.com/download/windows> and sign in. This gives the Windows
host Tailnet membership *and* keeps MagicDNS resolving for Windows-native apps
(your browser, §4).

**Step 5 — install Tailscale inside WSL2 too** (so SSH from inside the distro
and any CLI work resolves Tailnet names natively):

```bash
# inside WSL2 Ubuntu
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale status
ping -c1 surface.<tailnet>.ts.net
```

With mirrored networking, the WSL2 Tailscale and the Windows Tailscale coexist;
you authenticate each as its own node (or share auth — either is fine). The
install command is identical to the one `setup-jetson.sh` Phase 1.2 and
`setup-coordinator.sh` Phase 1.2 use.

> **You can do most operator work from either side.** Browser → web UI is
> simplest from **Windows** (the native browser + Windows Tailscale). SSH and
> `git` are simplest from **inside WSL2** (a normal Linux client). You do not
> have to pick one — both share the same MagicDNS names under mirrored mode.

---

## 3. SSH access

You SSH to two kinds of node:

- **The coordinator** — the WSL2 Ubuntu on the Surface. Log in as a
  sudo-capable WSL2 user (the account that ran `setup-coordinator.sh`). The
  unprivileged `turing` service user owns the running services; reach it with
  `sudo -u turing -H …` once you're in.
- **Each Jetson worker** — log in as the JetPack first-boot sudo user
  (`docs/hardware/jetson-orin-nano.md` uses `allen` as the example; substitute
  yours). The `turing` service user owns the worker checkout and service the
  same way.

### SSH key setup

Generate a key if you don't have one, then install your **public** key on each
node's `authorized_keys`.

#### On macOS

```bash
ssh-keygen -t ed25519 -C "operator-mac"          # if you don't already have one
ssh-copy-id <wsl2-user>@surface.<tailnet>.ts.net  # coordinator
ssh-copy-id allen@jetson-1.<tailnet>.ts.net        # each worker
# …repeat for jetson-2..4
```

#### On Windows + WSL2

Do this **inside WSL2** — it is a normal OpenSSH client, no different from a
Linux box:

```bash
# inside WSL2 Ubuntu
ssh-keygen -t ed25519 -C "operator-wsl"
ssh-copy-id <wsl2-user>@surface.<tailnet>.ts.net
ssh-copy-id allen@jetson-1.<tailnet>.ts.net
# …repeat for jetson-2..4
```

> **Note — WSL2 SSH is just Linux SSH.** Your `~/.ssh/` inside WSL2 is
> independent of `C:\Users\<you>\.ssh\` used by Windows OpenSSH / PuTTY. Keep
> your operator key inside WSL2 and run all `ssh`/`scp`/`rsync`/`git` from
> there; it behaves exactly like macOS. (If you prefer Windows-native SSH,
> that works too — it just uses the Windows key store.)

### `~/.ssh/config` host stanzas

Put this in `~/.ssh/config` (on macOS) or `~/.ssh/config` **inside WSL2** (on
Windows). It lets you type `ssh surface` / `ssh jetson-1`:

```sshconfig
# ── Turing fleet over the Tailnet ────────────────────────────────────
Host surface
    HostName surface.<tailnet>.ts.net
    User <your-wsl2-user>

Host jetson-1
    HostName jetson-1.<tailnet>.ts.net
    User allen
Host jetson-2
    HostName jetson-2.<tailnet>.ts.net
    User allen
Host jetson-3
    HostName jetson-3.<tailnet>.ts.net
    User allen
Host jetson-4
    HostName jetson-4.<tailnet>.ts.net
    User allen

# Shared niceties for all of the above
Host surface jetson-*
    ServerAliveInterval 30
    ServerAliveCountMax 4
```

Then:

```bash
ssh surface
ssh jetson-1
# On the coordinator, act as the service user:
ssh surface 'sudo -u turing -H bash -lc "cd ~/turing && git log -1"'
```

> **Tailscale SSH (optional).** If you enable Tailscale SSH on the nodes
> (`tailscale up --ssh` there, plus an ACL rule), Tailscale brokers the
> connection and you skip per-node `authorized_keys` entirely — auth is by
> Tailnet identity. This guide assumes plain OpenSSH because that's what the
> setup scripts provision; Tailscale SSH is an additive convenience, not a
> requirement.

---

## 4. Reaching the web UI

The web UI (the primary operator surface since ADR 0010) is served by the
**gateway**, which currently runs **in-process inside `turing-coordinator.service`**
on the Surface when `TURING_GATEWAY_ENABLED=true` (set by
`setup-coordinator.sh` Phase 4.2). Its URL over the Tailnet is:

```
http://surface.<tailnet>.ts.net:8765/
```

`8765` is the gateway default (`gateway_port` in `src/turing/config.py`; the
Windows firewall rule in `bootstrap-surface.ps1` opens exactly this port). The
gateway binds loopback by default (`gateway_bind`); the coordinator deployment
binds it to the Tailnet interface so the Surface's MagicDNS name reaches it.

### The bearer-token model

Every route except `/`, `/healthz`, `/peers`, and `/token-handoff` requires a
bearer token (`src/turing/gateway/auth.py`, `app.py` `_BearerMiddleware`). The
token is `TURING_GATEWAY_TOKEN` in the coordinator's `.env.coordinator`
(staged via the secrets bootstrap in `setup-coordinator.sh` Phase 4). Read it
on the coordinator:

```bash
ssh surface 'sudo grep -E "^TURING_GATEWAY_TOKEN=" /home/turing/turing/.env.coordinator'
```

Three ways to present it:

**(a) `turing-ui` launcher (recommended) — one-shot cookie handoff.** The
`turing-ui` console script (`pyproject.toml` → `[project.scripts]`,
`src/turing/launcher.py`) validates the token against `/healthz`, then opens
your browser at `/token-handoff?token=…`. The gateway sets an http-only cookie
(`turing_gateway_token`) and 303-redirects to `/`, so the token never lingers in
the address bar (`app.py` `token_handoff`). Run it **on whichever machine has
the browser**:

```bash
export TURING_GATEWAY_HOST=surface.<tailnet>.ts.net
export TURING_GATEWAY_PORT=8765
export TURING_GATEWAY_TOKEN=<paste from .env.coordinator>
turing-ui
```

(`turing-ui` defaults: `--port 8765`, host/token from the
`TURING_GATEWAY_HOST` / `TURING_GATEWAY_TOKEN` env vars — `launcher.py`.)

**(b) Plain browser.** Visit `http://surface.<tailnet>.ts.net:8765/` and complete
the login by hand via the one-shot handoff URL:

```
http://surface.<tailnet>.ts.net:8765/token-handoff?token=<your-token>
```

The bare `/` returns a friendly landing JSON pointing here until the cookie is
set (`app.py` `_LANDING_PAYLOAD`).

**(c) `curl` with an explicit header** (for API pokes / scripting):

```bash
curl -H "Authorization: Bearer $TURING_GATEWAY_TOKEN" \
     http://surface.<tailnet>.ts.net:8765/api/queue
```

### On macOS

Just open the browser at `http://surface.<tailnet>.ts.net:8765/`, or run
`turing-ui` (after `pip install -e .` in a checkout, or with the env vars above)
to do the cookie handoff for you.

### On Windows + WSL2

Use the **Windows browser** and the Surface's Tailnet name directly —
`http://surface.<tailnet>.ts.net:8765/`. With the Windows Tailscale client up
(§2 step 4) MagicDNS resolves natively for the browser. You can run `turing-ui`
from inside WSL2; under mirrored networking it can still reach the gateway, but
it will try to open a browser inside WSL2 — usually you just want to paste the
`/token-handoff?token=…` URL into your Windows browser instead.

### Fallback — SSH port-forward

If MagicDNS or the firewall rule is misbehaving, tunnel the gateway over SSH and
hit it on localhost (works identically on macOS and inside WSL2):

```bash
ssh -L 8765:localhost:8765 surface
# then browse to:
http://localhost:8765/token-handoff?token=<your-token>
```

> **Note — how the gateway is served.** By default the gateway runs **in-process
> under `turing-coordinator.service`** when `TURING_GATEWAY_ENABLED=true` (the
> default `setup-coordinator.sh` sets), and that is the surface with **live**
> telemetry/specs (it shares the coordinator's mesh). A dedicated
> `python -m turing.gateway` entrypoint (console script `turing-gateway`) now
> exists for ADR 0010 §6's three-unit split, but it is **opt-in**: the
> `turing-gateway.service` unit only execs it when `TURING_GATEWAY_STANDALONE=1`
> is set in `.env.coordinator` (otherwise it holds as a clean no-op so it never
> double-binds the port against the in-process gateway). Flipping it on — and
> setting `TURING_GATEWAY_ENABLED=false` to release the port — moves the gateway
> to its own unit; note the standalone process serves the SPA + queue/chat API
> and persists reward writes, but live queue/chat/telemetry projections remain in
> the coordinator process until the IPC split lands. For everyday use, keep the
> default in-process gateway.

### Terminal UI (`turing-tui`) — keyboard-first alternative

If you'd rather drive the cluster from a terminal than a browser, `turing-tui`
(Rust + ratatui, in `tui/`) is a standalone client of the **same gateway API**.
It mirrors the queue / chat / specs / trace / alert panes and uses the same
bearer token. Build it once (`cd tui && cargo build --release`), then from
**either macOS or WSL2**:

```bash
export TURING_GATEWAY_URL=http://surface.<tailnet>.ts.net:8765
export TURING_GATEWAY_TOKEN=...        # same token as the web UI
turing-tui                              # or: turing-tui --url ... --token ...
```

`https://` origins are upgraded to `wss://` automatically, and the WebSocket
auto-reconnects through coordinator restarts. `turing-tui --check` prints the
resolved config and exits. Keys and panes are documented in `tui/README.md`.

---

## 5. Provisioning the fleet (one command)

`scripts/setup-fleet.sh` is the fleet orchestrator. **Run it on the coordinator**
(inside WSL2 on the Surface), not on your laptop — it drives each Jetson over
SSH and pulls the worker NATS seeds from the coordinator's local key store. It
closes the two manual hand-offs `setup-jetson.sh` leaves to a human:

1. **NATS seed distribution.** `setup-coordinator.sh` Phase 5 wrote one seed per
   worker under `/etc/turing/nats/workers/worker-N.seed` (root, 0600).
   `setup-fleet.sh` reads `worker-N.seed` and feeds it to node *N* as
   `TURING_NATS_NKEY_SEED` — **no copy-paste**.
2. **Per-node GitHub auth.** The repo is private, so instead of a `gh` device
   flow on every Jetson, `--push-repo` (the default) **rsyncs the coordinator's
   checked-out tree** to each node and runs `setup-jetson.sh` with
   `TURING_DEPLOY_SRC` + `TURING_SKIP_GH_AUTH`.

### Hosts are positional, in worker order

The hostnames are given **in worker order**: the first host gets
`worker-1.seed`, the second `worker-2.seed`, and so on. Pass them positionally,
via `--hosts "…"`, or via `$TURING_FLEET_HOSTS`.

### Recommended first-run command

From inside WSL2 on the Surface, after `setup-coordinator.sh` has run (so the
seeds and `.env.coordinator` exist):

```bash
cd ~/turing
scripts/setup-fleet.sh jetson-1 jetson-2 jetson-3 jetson-4
```

This is a full, hands-off bring-up: `--push-repo` is the default (private-repo
rsync deploy, no per-node `gh` auth), seeds are auto-distributed from
`/etc/turing/nats/workers/`, and the coordinator NATS URL is resolved
automatically (from `--nats-url`, else `.env.coordinator`'s `TURING_NATS_URL`,
else this node's Tailscale DNS name → `tls://…:4222`). Nodes provision
sequentially by default.

### Update command (code refresh + restart only)

```bash
scripts/setup-fleet.sh --update --parallel jetson-1 jetson-2 jetson-3 jetson-4
```

`--update` is a light refresh: rsync the code and `systemctl restart turing` on
each node, skipping the package/model/auth phases (it implies `--push-repo`).
`--parallel` provisions all nodes concurrently with per-node logs.

### Useful options

| Option | Effect |
|---|---|
| `--push-repo` | Deploy by rsync from the coordinator tree (default; private-repo friendly, no per-node `gh` auth). |
| `--via-clone` | Each node clones from GitHub instead (needs the node `gh`-authed; falls back to the interactive flow). |
| `--update` | Code refresh + restart only; skips package/model/auth phases. Implies `--push-repo`. |
| `--parallel` | Provision all nodes at once (default is sequential, so a first-time `--via-clone` device flow can be done node by node). |
| `--dry-run` | Print the plan and exact remote commands; change nothing. |
| `--user USER` | SSH user on each Jetson (default `allen`, or `$TURING_FLEET_USER`). |
| `--nats-url URL` | Override the coordinator NATS URL handed to every worker. |
| `--seed-dir DIR` | Where the durable worker seeds live (default `/etc/turing/nats/workers`). |

Preview before touching anything:

```bash
scripts/setup-fleet.sh --dry-run jetson-1 jetson-2 jetson-3 jetson-4
```

Source: `scripts/setup-fleet.sh` (header + arg parser); the per-node worker
script it drives is `scripts/setup-jetson.sh` (Phase 4 grows
`TURING_NATS_URL` + `TURING_NATS_NKEY_SEED`, ADR 0010 §9).

---

## 6. NATS bus

The runtime bus is `nats-server` on the coordinator, reachable over the Tailnet
at:

```
tls://surface.<tailnet>.ts.net:4222
```

- **TLS + nkey auth, LAN/Tailnet-only.** `setup-coordinator.sh` Phase 5 mints a
  self-signed TLS cert (with a SAN covering the Tailnet DNS name + loopback) and
  authorizes 5 nkey public keys — 1 coordinator + 4 worker
  (`CONTEXT.md` → Runtime bus; ADR 0010 §5). `4222` is the listen port
  (`NATS_PORT` in the coordinator script; `nats_url` default in `config.py` is
  `nats://127.0.0.1:4222` for local dev, overridden to the `tls://…` Tailnet URL
  in production).
- **A worker's `.env` carries the link.** Each Jetson's
  `/home/turing/turing/.env` has `TURING_NATS_URL` (the URL above) and
  `TURING_NATS_NKEY_SEED` (that worker's seed). The worker systemd unit
  **refuses to start** if either is empty (`setup-jetson.sh` Phase 5
  `ExecStartPre`, ADR 0010 §9). `setup-fleet.sh` fills both in for you (§5).

### Quick connectivity check

You typically do not connect to NATS as an operator — workers do. But to confirm
the port is reachable and the TLS listener is up, from your laptop (Tailnet
member) or any worker:

```bash
# Port reachable?
nc -vz surface.<tailnet>.ts.net 4222

# TLS handshake + inspect the cert SAN (no auth needed to see the handshake):
openssl s_client -connect surface.<tailnet>.ts.net:4222 </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -ext subjectAltName
```

A successful `nc` and a cert whose `subjectAltName` includes
`surface.<tailnet>.ts.net` means the transport is good; the rest is nkey auth,
which the workers' seeds handle. If the SAN does *not* list the name you dialed,
see the troubleshooting table (TLS SAN mismatch is the classic NATS handshake
failure here).

> **Note — on the worker side, `setup-coordinator.sh`'s Phase 5 output shows
> `nats://…`** in its example prompts, while the server actually listens on
> `tls://`. Workers must use `tls://surface.<tailnet>.ts.net:4222` to match the
> TLS listener; the `tls://` URL is what the coordinator writes into
> `.env.coordinator` and what `setup-fleet.sh` distributes.

---

## 7. Vault git remote

The vault is a **private git repository** (ADR 0010 §4). The coordinator's WSL2
working tree (`/home/turing/vault`) is the **single writer** — it commits on
curated promotions, and the commit log is the reward-signal audit trail. Every
other device, **including your Mac, is a read-only `git pull` remote**.

> **Single-writer rule (non-negotiable).** Do not `git push` from your laptop.
> Do not run **Obsidian Sync** on the same vault — Obsidian Sync + the
> coordinator's git are two writers to the same files and will silently
> overwrite each other and corrupt the audit trail. Pick git; turn Obsidian Sync
> **off** for this vault.

### On macOS (and identically inside WSL2)

```bash
# One-time clone (read-only consumer):
git clone git@github.com:<your-username>/turing-vault.git ~/Vaults/turing-vault

# Point Obsidian at ~/Vaults/turing-vault and turn OFF Obsidian Sync for it.

# Refresh with the coordinator's latest curated promotions:
cd ~/Vaults/turing-vault
git pull --ff-only
```

`--ff-only` is deliberate: a refused fast-forward means a second writer touched
the repo — investigate, do not `merge`/`push` to "fix" it. The full runbook
(creating the private repo, seeding from existing notes, the coordinator clone,
the watcher) is in **`docs/operator/vault-git-workflow.md`** — read it before
first use.

---

## 8. Diagnostics

All `journalctl`/`systemctl` commands run **on the node**, via SSH. Prefix with
`sudo` (the units run as root-installed services).

### On a worker (Jetson)

```bash
ssh jetson-1
sudo journalctl -u turing -f          # follow the worker agent loop
sudo systemctl status turing          # active (running)?
jtop                                  # GPU/CPU/mem/thermals TUI (jetson-stats)
```

### On the coordinator (Surface / WSL2)

```bash
ssh surface
sudo journalctl -u turing-coordinator -f      # agent loop, scheduler, alerts, in-process gateway
sudo journalctl -u turing-gateway -f          # the (currently no-op) reserved gateway unit
sudo journalctl -u turing-vault-watcher -f    # vault reindex (vault_watcher.reindexed lines)
sudo journalctl -u nats-server -f             # the runtime bus
sudo journalctl -u ntfy -f                    # the alert push server
sudo systemctl status nats-server ntfy --no-pager
tailscale status                              # confirm WSL2 is on the Tailnet
```

(The three `turing-*` units are ADR 0010 §6; `setup-coordinator.sh` Phase 7
installs them.)

### ntfy alert topic (closed-laptop pushes)

Hardware-safety alerts (temp/disk) push to your ntfy topic when the web UI's
telemetry sink has gone stale (no SPA connected for 90 s) — the closed-laptop
fallback (ADR 0010 §2, `CONTEXT.md` → Alerts). Subscribe the ntfy mobile app to
your topic and smoke-test it from any worker:

```bash
curl -d "connectivity smoke test" \
     http://surface.<tailnet>.ts.net:8090/turing-alerts-<operator>
```

The topic is `TURING_OPERATOR_NTFY_TOPIC` and the server listens on `:8090`
(`scripts/coordinator/server.yml` `listen-http`; `docs/coordinator-ntfy.md`).
Full ntfy setup + troubleshooting: `docs/coordinator-ntfy.md`.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `surface.<tailnet>.ts.net` won't resolve | MagicDNS off, or laptop not on the tailnet | Tailscale admin → DNS → MagicDNS: enabled. `tailscale status` to confirm membership; `tailscale ip -4` for an IP. |
| Coordinator / Jetsons unreachable from WSL2 even though Windows Tailscale is up | `.wslconfig` not `networkingMode=mirrored`, or `wsl --shutdown` not run | Set mirrored mode (§2 step 1), `wsl --shutdown`, restart the distro. On Win11 < 22H2 (build 22621) mirrored may not work — use the coordinator's NAT/port-proxy fallback (`deploy/scripts/wsl-portproxy.ps1`; ADR 0010 §5 Consequences). |
| `tailscaled`/services won't run inside WSL2; `systemctl` says "offline" | systemd not enabled in WSL2 | `/etc/wsl.conf` → `[boot] systemd=true`, then `wsl --shutdown`. Verify `systemctl is-system-running`. |
| Web UI returns 401 / "unauthorized" | Wrong or missing bearer token | Re-read `TURING_GATEWAY_TOKEN` from `.env.coordinator` (§4); re-run `turing-ui` or hit `/token-handoff?token=…` to reset the cookie (`turing_gateway_token`). |
| Web UI page is a JSON landing blob, not the app | No cookie yet (or no SPA bundle built) | Complete the handoff (§4 a/b). If it persists, the SPA bundle may not be built into the wheel — check `turing-coordinator` logs for `gateway_started`. |
| Worker won't start; journal shows `TURING_NATS_URL`/`TURING_NATS_NKEY_SEED` missing | Empty NATS fields in the worker `.env` | These are required (ADR 0010 §9). Re-run `setup-fleet.sh <hosts>` from the coordinator to refill them, or paste manually from `/etc/turing/nats/workers/`. |
| NATS handshake fails / TLS verify error | Cert SAN doesn't cover the dialed name, or worker used `nats://` not `tls://` | Confirm with `openssl s_client … | openssl x509 -ext subjectAltName` (§6) that the SAN lists `surface.<tailnet>.ts.net`. Worker URL must be `tls://…:4222`. If the Tailnet name changed after cert mint, regenerate: delete `/etc/turing/nats/coordinator.seed` and re-run `setup-coordinator.sh` (re-issues all seeds + cert). |
| Intermittent auth / TLS / "token expired" oddities | Clock skew between nodes | Check `timedatectl` on each node; ensure NTP sync. Large skew breaks TLS validity windows and time-based checks. |
| A Jetson never appears as a worker / peer | Not on the Tailnet, or SSH user wrong | `tailscale status` on the Jetson; confirm the `--user` (`allen`) matches the JetPack account. `setup-fleet.sh` preflights SSH and warns per node. |
| `ntfy` push accepted but no phone notification | Phone subscribed to wrong topic / wrong base-url | Subscribe the app to the exact `TURING_OPERATOR_NTFY_TOPIC` at the coordinator's base-url (not `ntfy.sh`). See `docs/coordinator-ntfy.md` Appendix A. |
| `vault git pull` refuses to fast-forward | A second writer touched the vault (Obsidian Sync still on, or a stray push) | Stop the second writer; never `merge`/`push` from a consumer device. See `docs/operator/vault-git-workflow.md`. |

---

## See also

- `docs/adr/0009-jetson-fleet-and-research-flywheel.md` — Surface/WSL2
  coordinator rationale, power settings, mirrored networking + systemd-in-WSL2
  requirements.
- `docs/adr/0010-discord-retirement-and-coordinator-bringup.md` — gateway /
  web UI as the primary surface, ntfy, the three coordinator systemd units,
  NATS §5/§9, vault git §4, Tailscale-in-WSL2 §8.
- `docs/operator/vault-git-workflow.md` — full vault git runbook (private repo,
  single-writer, Mac pull-only).
- `docs/coordinator-ntfy.md` — ntfy server setup, topic, firewall, smoke test.
- `docs/hardware/jetson-orin-nano.md` — per-Jetson provisioning, SSH user
  convention, `jtop`.
- `scripts/setup-fleet.sh`, `scripts/setup-coordinator.sh`,
  `scripts/setup-jetson.sh`, `scripts/bootstrap-surface.ps1` — the provisioning
  scripts this guide drives.
- `CONTEXT.md` — topology, runtime bus, operator surfaces, alerts, vault.
