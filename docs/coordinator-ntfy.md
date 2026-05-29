# ntfy on the Surface Coordinator — Closed-Laptop Alert Push

Self-hosting [ntfy](https://ntfy.sh) inside WSL2 on the Surface coordinator so
hardware-safety alerts (PRD #228) reach the operator's phone when the webui is
out of reach. This is the **closed-laptop fallback** that ADR-0010 §2 chose to
replace the retired Discord-DM fallback: the SPA top banner remains the primary
alert channel; ntfy fires only when the SPA telemetry sink has gone stale.

This doc is slice A of ADR-0010. It stands up ntfy as config-as-code:
the systemd unit, the topic config, and the Windows Firewall rule. The
alerts dispatcher transport swap (Discord → ntfy) is **slice B** and is
out of scope here; the dispatcher does not post to ntfy yet.

> ntfy lives **inside WSL2**, next to NATS and the coordinator (ADR-0010 §8).
> The Windows host is not a Tailnet node — Tailnet reachability is carried by
> WSL2 mirrored networking (ADR-0010 §1, §5). Workers never post alerts;
> `TURING_OPERATOR_NTFY_TOPIC` is a coordinator-only setting.

---

## Overview

What you end up with after following this doc:

- An `ntfy` server running inside WSL2 Ubuntu on the Surface, under a
  dedicated `ntfy` service user, managed by `systemd` (`ntfy.service`,
  `Restart=always`, enabled at boot).
- A per-operator topic set in the coordinator's `.env` as
  `TURING_OPERATOR_NTFY_TOPIC`.
- A Windows Firewall inbound rule that admits ntfy's port **from the local
  Tailnet subnet only** — Jetsons (and the operator's phone via the tailnet)
  can reach it; the open internet cannot.
- A `curl`-verified push that lands on the operator's ntfy client (phone app
  or web).

### Prerequisites

Before you start:

- A Surface running Windows with WSL2 Ubuntu and `systemd` enabled
  (`/etc/wsl.conf` has `[boot] systemd=true` — slice F / `bootstrap-surface.ps1`
  sets this; until then set it by hand and `wsl --shutdown`).
- Tailscale installed and authenticated **inside WSL2** (`tailscale up`), so
  the coordinator has a stable tailnet IP and the Jetsons can reach it.
- The ntfy mobile app (or any ntfy client) installed on the operator's phone
  and subscribed to the topic chosen below.
- `sudo` inside WSL2.

### Quick start (systemd install)

Once `setup-coordinator.sh` lands (slice G) this is one phase of that script.
Until then, from inside WSL2 Ubuntu:

```bash
# 1. Install the ntfy server (Debian/Ubuntu package)
curl -sSL https://archive.ntfy.sh/apt/ntfy.gpg \
  | sudo tee /etc/apt/keyrings/ntfy.gpg >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/ntfy.gpg] https://archive.ntfy.sh/apt /" \
  | sudo tee /etc/apt/sources.list.d/ntfy.list >/dev/null
sudo apt update && sudo apt install -y ntfy

# 2. Drop the config-as-code unit from this repo
sudo install -m 644 scripts/coordinator/ntfy.service \
  /etc/systemd/system/ntfy.service
sudo systemctl daemon-reload
sudo systemctl enable --now ntfy

# 3. Confirm it came up
systemctl status ntfy --no-pager
```

The rest of this doc is the manual walkthrough plus the firewall step (a
Windows-side action that cannot live in a WSL2 bash script) and the
end-to-end verification.

### Phases

1. Install the ntfy server (inside WSL2)
2. Configure the topic (`TURING_OPERATOR_NTFY_TOPIC`)
3. Install the systemd unit (idempotent)
4. Open the Windows Firewall inbound rule (Tailnet subnet only)
5. Verify with a `curl` push from a Jetson

Plus one appendix: troubleshooting.

---

## Phase 1 — Install the ntfy server

Inside WSL2 Ubuntu, as a sudo-capable user. ntfy ships a Debian/Ubuntu
package; prefer it over a raw binary so apt owns updates.

```bash
sudo mkdir -p -m 755 /etc/apt/keyrings
curl -sSL https://archive.ntfy.sh/apt/ntfy.gpg \
  | sudo tee /etc/apt/keyrings/ntfy.gpg >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/ntfy.gpg] https://archive.ntfy.sh/apt /" \
  | sudo tee /etc/apt/sources.list.d/ntfy.list >/dev/null
sudo apt update
sudo apt install -y ntfy
ntfy --version
```

The package creates the `ntfy` service user that the unit in this repo runs
as, and a stock `/etc/ntfy/server.yml`. We overwrite that stock config with the
repo's `scripts/coordinator/server.yml` in Phase 2, and replace the package's
own unit with the repo's config-as-code unit in Phase 3.

> **Idempotent re-install.** Re-running these commands is safe: `apt install`
> on an already-installed package is a no-op, and the keyring/source writes
> overwrite identical content. To reinstall from scratch:
> `sudo apt purge -y ntfy && sudo rm -f /etc/systemd/system/ntfy.service`,
> then re-run this doc.

---

## Phase 2 — Configure the topic

ntfy topics are namespaces: anyone who knows the topic name can publish and
subscribe, so treat the name as a shared secret. Pick a hard-to-guess,
per-operator topic.

### 2.1 ntfy server config

The server config is config-as-code at `scripts/coordinator/server.yml` in this
repo — install it to `/etc/ntfy/server.yml` (the path
`scripts/coordinator/ntfy.service` loads via `--config`) and edit `base-url` to
your tailnet name:

```bash
sudo install -m 644 scripts/coordinator/server.yml /etc/ntfy/server.yml
sudoedit /etc/ntfy/server.yml   # set base-url to the WSL2 tailnet name
```

The two settings that matter for the Tailnet-only posture:

```yaml
# /etc/ntfy/server.yml (tracked at scripts/coordinator/server.yml)
base-url: "http://surface.<your-tailnet>.ts.net"   # the WSL2 tailnet name
listen-http: ":8090"                               # the port the firewall admits
```

`listen-http` is the port the Windows Firewall rule in Phase 4 opens. ntfy's
default is `:80`; we use a non-privileged custom port (`:8090` here) so the
`ntfy` service user can bind it without `CAP_NET_BIND_SERVICE` and so the
firewall rule is narrowly scoped. **Use the same port in both places** — the
`server.yml` `listen-http` and the firewall `LocalPort` in Phase 4. Because the
port contract lives in the tracked template, the firewall rule and the smoke
test can be audited against one source of truth.

> **Plaintext HTTP on `:8090` is intentional.** Tailscale/WireGuard encrypts all
> transport over the `100.64.0.0/10` CGNAT range, so the listener never faces the
> open internet and the topic name is the only access control. Enabling ntfy TLS
> is an optional hardening step (see the ntfy docs); the firewall scoping in
> Phase 4 is what keeps the plaintext listener tailnet-only.

### 2.2 Coordinator `.env`

Set the topic on the coordinator's `.env` (coordinator only — this is the only
node that posts alerts). The field is `operator_ntfy_topic` in
`src/turing/config.py`:

```bash
TURING_OPERATOR_NTFY_TOPIC=turing-alerts-<operator>
```

Leave it unset (or commented) to disable the ntfy fallback — same semantics as
the old `TURING_OPERATOR_DISCORD_ID`. The alerts dispatcher reads this topic in
slice B; setting it now is harmless (no consumer yet).

---

## Phase 3 — Install the systemd unit

The unit is config-as-code at `scripts/coordinator/ntfy.service` in this repo.
Install it idempotently — mirroring the `setup-jetson.sh` install pattern
(write to a temp, compare, only daemon-reload on change):

```bash
UNIT_SRC=scripts/coordinator/ntfy.service
UNIT_DST=/etc/systemd/system/ntfy.service
if [[ -f "$UNIT_DST" ]] && sudo cmp -s "$UNIT_SRC" "$UNIT_DST"; then
    echo "unit unchanged, skipping"
else
    sudo install -m 644 "$UNIT_SRC" "$UNIT_DST"
    sudo systemctl daemon-reload
fi
sudo systemctl enable --now ntfy
systemctl status ntfy --no-pager
```

`enable --now` both enables-at-boot and starts immediately; re-running it on an
already-enabled, already-active unit is a no-op. The unit has `Restart=always`
and `RestartSec=10`, so it rides through Surface reboots alongside the three
`turing-*` units (ADR-0010 §6).

> **Re-install / upgrade.** When the unit in the repo changes, re-running the
> snippet above detects the diff via `cmp`, reinstalls, and `daemon-reload`s;
> an unchanged unit is skipped. To force a clean restart after a config edit:
> `sudo systemctl restart ntfy`.

---

## Phase 4 — Open the Windows Firewall inbound rule

ntfy listens inside WSL2, but with **mirrored networking** the WSL2 listener
shares the Windows host's network namespace, so inbound traffic is governed by
the **Windows** Firewall, not WSL2's. Open ntfy's port for the **local Tailnet
subnet only** — Tailscale's CGNAT range `100.64.0.0/10` — so Jetsons and the
operator's phone (over the tailnet) can reach it while the open internet
cannot.

This is a Windows-side action and cannot live in a WSL2 bash script. Run it in
an **elevated PowerShell** on the Windows host. It folds into
`scripts/bootstrap-surface.ps1` in slice F; for now it is a documented manual
step.

```powershell
# Windows Firewall — inbound ntfy, scoped to the Tailnet subnet ONLY.
# Run in an elevated PowerShell on the Surface (Windows side, not WSL2).
# RemoteAddress restricts the rule to Tailscale's CGNAT range (100.64.0.0/10),
# so only tailnet peers (Jetsons, the operator's phone) can reach ntfy.
# Keep the LocalPort in sync with `listen-http` in /etc/ntfy/server.yml.
#
# Idempotent: remove any prior rule of the same name before re-adding.
$ruleName = "Turing ntfy (Tailnet inbound)"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule

New-NetFirewallRule `
    -DisplayName $ruleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort 8090 `
    -RemoteAddress 100.64.0.0/10 `
    -Profile Any
```

> **Scope it to the tailnet, not "any".** `RemoteAddress 100.64.0.0/10` is the
> entire Tailscale CGNAT space; tighten it to your tailnet's actual subnet if
> you have one. Do **not** open the port to `Any` remote address — ntfy topics
> are unauthenticated by default, so a publicly reachable port would let anyone
> who guesses the topic name push alerts (or read them).

`netsh` equivalent, if you prefer it (same scoping):

```powershell
netsh advfirewall firewall add rule `
    name="Turing ntfy (Tailnet inbound)" `
    dir=in action=allow protocol=TCP localport=8090 `
    remoteip=100.64.0.0/10
```

---

## Phase 5 — Verify with a `curl` push from a Jetson

From any Jetson worker (which is on the tailnet), publish a test message to the
topic. Use the coordinator's tailnet name and the port from Phase 2/4:

```bash
curl -d "ntfy slice A smoke test" \
    http://surface.<your-tailnet>.ts.net:8090/turing-alerts-<operator>
```

A `{"id":...,"time":...,"event":"message",...}` JSON response means ntfy
accepted the publish. Within a second or two the push should land on the
operator's phone (or whatever ntfy client is subscribed to the topic).

If the `curl` hangs or is refused, work down the appendix — most failures are
the firewall rule, the port mismatch, or Tailscale not up inside WSL2.

> **What this does and does not prove.** A successful push confirms the
> transport (ntfy server + firewall + tailnet + client subscription). It does
> **not** exercise the alerts dispatcher — that integration is slice B. The
> cold-start coordinator ntfy push (ADR-0010 §6) also lands in a later slice.

---

## Appendix A — Troubleshooting

### `curl` from a Jetson hangs or is connection-refused

- Confirm ntfy is listening inside WSL2:
  `sudo ss -ltnp | grep ntfy` — should show the port from `listen-http`.
- Confirm the port matches everywhere: `listen-http` in `/etc/ntfy/server.yml`,
  `LocalPort` in the firewall rule, and the `curl` URL all use the same number.
- Confirm the firewall rule exists and is scoped right (elevated PowerShell):
  `Get-NetFirewallRule -DisplayName "Turing ntfy (Tailnet inbound)" | Get-NetFirewallAddressFilter`.
- Confirm both ends are on the tailnet: `tailscale status` on the Jetson and
  inside WSL2 on the Surface.

### Push accepted (200 JSON) but no notification on the phone

- Confirm the phone's ntfy app is subscribed to the **exact** topic name
  (case-sensitive) and pointed at the coordinator's `base-url`, not the public
  `ntfy.sh`.
- Topic names are the only access control by default — a typo means you're
  publishing to a different (empty) topic.

### `systemctl status ntfy` shows the unit failed

```bash
sudo journalctl -u ntfy -n 100 --no-pager
```

Most common causes:
- `/etc/ntfy/server.yml` has a YAML syntax error → ntfy refuses to start.
- The configured port is already in use → change `listen-http`.
- The `ntfy` service user can't read `server.yml` →
  `sudo chown root:ntfy /etc/ntfy/server.yml && sudo chmod 640 /etc/ntfy/server.yml`.

### Mirrored networking not in effect

If inbound traffic doesn't reach the WSL2 listener even with the firewall open,
confirm `.wslconfig` has `networkingMode=mirrored` and that you ran
`wsl --shutdown` after setting it (slice F automates this). On Windows builds
without mirrored-networking support the fallback is NAT mode plus a
`netsh interface portproxy` rule — see ADR-0010 Consequences.

---

## References

- ADR-0010 §2 (closed-laptop alert fallback: ntfy), §6 (coordinator process
  layout — `ntfy.service`), §8 (ntfy location: inside WSL2).
- `docs/adr/0010-slices.md` — slice A (this doc), slice B (dispatcher swap),
  slice F (`bootstrap-surface.ps1` folds in the firewall rule), slice G
  (`setup-coordinator.sh` folds in the ntfy install).
- PRD #228 — hardware-safety alerts state machine and 90 s sink-stale trigger.
- `scripts/coordinator/ntfy.service` — the config-as-code systemd unit.
- `scripts/coordinator/server.yml` — the config-as-code ntfy server config
  (`base-url`, `listen-http :8090`, cache) the unit loads via `--config`.
