# Surface coordinator bootstrap — Windows side (ADR 0010 §5, issue #279)

This is the operator runbook for [`scripts/bootstrap-surface.ps1`](../scripts/bootstrap-surface.ps1),
the **Windows-side** half of bringing up the Surface/WSL2 coordinator. It gets
the Windows host to the point where [`scripts/setup-coordinator.sh`](../scripts/setup-coordinator.sh)
(Slice G) can run inside a known-good WSL2 Ubuntu.

> **Split of concerns.** Everything Tailnet-facing — Tailscale, NATS, ntfy —
> lives **inside WSL2** (ADR 0010 §8). The Windows host is *not* a Tailnet node.
> This script only enables WSL2, installs Ubuntu, configures networking +
> systemd + power + firewall, and registers the boot-time keep-alive. It
> installs no services on the Windows host itself.

The script is **idempotent**: every step is check-first, so a second run prints
`skip:` / `already …` for each step and changes nothing. The config files it
writes are byte-compared before any rewrite; the firewall rules and Task
Scheduler entry are checked for existence (and re-converged if drifted).

---

## Prerequisites

- A Surface Pro (16 GB) on Windows 11 22H2 (build **22621**) or newer for clean
  mirrored networking. Windows 10 / older Windows 11 still works via the NAT
  fallback (see [§Fallback](#fallback-mirrored-networking-unavailable)).
- An **elevated** PowerShell. Feature-enable, firewall, and Task Scheduler all
  require admin; the script refuses to run otherwise.
- Internet access (the Ubuntu rootfs is downloaded on first install).

## Run it

```powershell
# From an ELEVATED PowerShell, at the repo root:
powershell -ExecutionPolicy Bypass -File scripts\bootstrap-surface.ps1
```

Optional parameters (defaults shown):

```powershell
scripts\bootstrap-surface.ps1 `
    -DistroName "Ubuntu" `              # WSL2 distro name
    -TailnetSubnet "100.64.0.0/10" `    # Tailscale CGNAT range; override for a custom tailnet CIDR
    -TaskName "Turing WSL2 Boot"        # Task Scheduler entry name
```

If enabling the WSL2 / Virtual Machine Platform features requires a reboot
(fresh Windows install), the script says so and stops before installing Ubuntu.
**Reboot, then re-run** — it picks up where it left off and installs the distro,
writes `/etc/wsl.conf`, etc.

## What it does, step by step

| # | Step | Idempotency check |
|---|------|-------------------|
| 1 | Enable `Microsoft-Windows-Subsystem-Linux` + `VirtualMachinePlatform` | `Get-WindowsOptionalFeature … .State -eq "Enabled"` → skip |
| 2 | `wsl --set-default-version 2`; install Ubuntu (`--no-launch`) | `wsl --list --quiet` contains the distro → skip |
| 3 | Write `C:\Users\<you>\.wslconfig` with `[wsl2] networkingMode=mirrored` + `[experimental] hostAddressLoopback=true` | byte-compare existing file → skip if identical |
| 4 | Write `/etc/wsl.conf` inside the distro with `[boot] systemd=true` | `grep` for `systemd=true` inside the distro → skip |
| 5 | Power plan: no sleep/hibernate (AC + DC), do-nothing on lid close | `powercfg` re-assert is inherently a no-op |
| 6 | Firewall inbound for NATS/gateway/ntfy, scoped to the Tailnet subnet | rule exists **and** remote scope matches → skip; re-scope if drifted |
| 7 | Task Scheduler `Turing WSL2 Boot`: `wsl -d Ubuntu` at startup, restart-on-exit | task exists → re-assert definition (idempotent) |

### Ports opened (all served inside WSL2, Tailnet-facing)

| Service | Port(s) | Protocol |
|---------|---------|----------|
| NATS    | 4222    | TCP |
| gateway | 8765    | TCP |
| ntfy    | 80, 443 | TCP |

All rules are **inbound only** and scoped to the Tailnet subnet
(`100.64.0.0/10`, the Tailscale CGNAT range) — not the whole LAN, not the
public internet. (ADR 0010 §5.)

### Always-on posture (ADR 0010 §7)

- **Power**: never sleep on AC; do-nothing on lid close. A coordinator that
  sleeps takes the whole fleet's brain offline.
- **Boot**: the `Turing WSL2 Boot` scheduled task runs `wsl -d Ubuntu` at system
  startup (as `SYSTEM`, no autologon) and restarts it every minute if it exits.
- **Windows Update is not suppressed**: the coordinator rides through reboots
  via systemd + auto-start. After a reboot the task re-launches WSL2; systemd
  (PID 1) starts the `turing-coordinator/gateway/vault-watcher` units; the
  coordinator unit fires a single cold-start ntfy push.

## After it finishes

```powershell
wsl --shutdown                 # so .wslconfig + systemd=true take effect
wsl -d Ubuntu                  # boot the distro
# inside the distro:
#   bash scripts/setup-coordinator.sh   (Slice G)
```

Verify systemd is PID 1:

```powershell
wsl -d Ubuntu -- systemctl is-system-running   # "running" or "degraded" (not "offline")
wsl -d Ubuntu -- ps -p 1 -o comm=               # systemd
```

---

## Fallback: mirrored networking unavailable

Mirrored networking is a Windows 11 22H2+ feature. On an older build — or any
host where mirrored mode does not carry Tailnet reachability cleanly — WSL2
falls back to its **default NAT**, and the coordinator's ports bound inside the
distro are *not* reachable from the Tailnet at the Surface's IP. The script
warns about this when it detects a pre-22621 build, but still writes
`.wslconfig` and the firewall rules so the fallback is one command away.

**Workaround:** run [`deploy/scripts/wsl-portproxy.ps1`](scripts/wsl-portproxy.ps1)
from an elevated PowerShell. It reads the WSL2 internal IP fresh
(`wsl hostname -I`) and forwards the Windows LAN interface to it:

```powershell
deploy\scripts\wsl-portproxy.ps1
```

> **WSL2's internal IP changes across reboots in NAT mode**, so re-run the
> port-proxy after every reboot. (Mirrored mode does not have this problem,
> which is why it is preferred.) The bootstrap firewall rules already permit the
> inbound ports; the port-proxy only adds the `netsh interface portproxy`
> forwarding layer.

### Manual port-forwarding table (if you script your own proxy)

The existing `wsl-portproxy.ps1` forwards NATS (4222) and gateway (8765). If you
also need ntfy reachable from the Tailnet under NAT, extend the `$ports` array
to include `80, 443`, or add the rows manually:

| Listen (Windows) | → Connect (WSL2 IP) | Service |
|------------------|---------------------|---------|
| `0.0.0.0:4222`   | `<wsl-ip>:4222`     | NATS |
| `0.0.0.0:8765`   | `<wsl-ip>:8765`     | gateway |
| `0.0.0.0:80`     | `<wsl-ip>:80`       | ntfy (http) |
| `0.0.0.0:443`    | `<wsl-ip>:443`      | ntfy (https) |

```powershell
# One row, by hand (repeat per port; <wsl-ip> from `wsl hostname -I`):
netsh interface portproxy add v4tov4 listenport=4222 listenaddress=0.0.0.0 `
    connectport=4222 connectaddress=<wsl-ip>
# Inspect the active table:
netsh interface portproxy show v4tov4
```

The Tailnet-scoped inbound firewall rules from step 6 cover the inbound side;
the port-proxy only adds host→distro forwarding.

---

## Acceptance criteria

### Codeable (verified in code / by the script's own checks)

- [x] Script runs without errors on clean Windows 11 with WSL2 not yet installed
      (features enabled, reboot-then-rerun handled).
- [x] Re-run is a clean no-op: every step prints `skip:` / `already …` and
      executes no mutating command (config byte-compared, firewall scope
      verified, scheduled task re-asserted to the same definition).
- [x] `.wslconfig` written to `C:\Users\<you>\.wslconfig` with
      `[wsl2] networkingMode=mirrored` and `[experimental] hostAddressLoopback=true`.
- [x] `/etc/wsl.conf` written inside the distro with `[boot] systemd=true`.
- [x] Firewall inbound rules for NATS (4222), gateway (8765), ntfy (80/443),
      each scoped to the Tailnet subnet only.
- [x] Task Scheduler entry `Turing WSL2 Boot` exists and is enabled; runs
      `wsl -d Ubuntu` at system startup with restart-on-exit.
- [x] Power: no sleep on AC, do-nothing on lid close
      (`powercfg /q SCHEME_CURRENT SUB_BUTTONS` confirms).
- [x] This doc records all steps, the fallback flow (mirrored → NAT + port-proxy),
      and the manual port-forwarding table.
- [x] Windows 10 / pre-22H2 handled: the script warns and points at the NAT
      fallback rather than failing.

### Hardware-gated (must be confirmed on the real Surface)

These cannot run here — they require Windows + the physical Surface + a Jetson
on the Tailnet. Itemized for the operator:

- [ ] Bootstrap runs start-to-finish on a clean Surface Pro 16 GB; first WSL2
      boot completes and the distro is responsive
      (`wsl -d Ubuntu -- id`).
- [ ] **Second run is a clean no-op**: no feature reinstalls, no firewall rule
      rewrites, no Task Scheduler recreation — only `skip:` lines.
- [ ] After a system reboot, the `Turing WSL2 Boot` task runs automatically and
      the distro starts with no user intervention.
- [ ] systemd is running inside the distro after bootstrap
      (`wsl -d Ubuntu -- systemctl is-system-running` → `running` or `degraded`).
- [ ] A Jetson on the Tailnet can reach NATS and the gateway at the Surface IP
      (`nc -vz <surface-ip> 4222` and `8765` succeed).
- [ ] Mirrored networking carries Tailnet reachability both ways; the
      Tailnet-scoped firewall rules allow Jetson→coordinator inbound on all
      three services.
- [ ] **Fallback tested**: with mirrored networking unavailable, running
      `deploy/scripts/wsl-portproxy.ps1` restores port forwarding and the Jetson
      reaches the coordinator.
- [ ] ntfy topic receives a test push from inside WSL2 / from a Jetson
      (`curl -d 'test' https://ntfy.<surface-tailnet>/<topic>` succeeds) — once
      Slice A's ntfy server is up.

## Troubleshooting

- **"Run this from an ELEVATED PowerShell"** — re-launch PowerShell as
  Administrator. All of features / firewall / Task Scheduler need admin.
- **`wsl --install` says a reboot is required** — that's expected on a fresh
  host. Reboot, re-run the script.
- **Jetson can't reach `nats://<surface-ip>:4222`** even though it works inside
  WSL — you're almost certainly in NAT mode, not mirrored. Confirm `.wslconfig`
  has `networkingMode=mirrored`, `wsl --shutdown`, restart; if the build can't
  do mirrored, use the NAT fallback above.
- **Task fires but nothing comes up** — confirm `/etc/wsl.conf` has
  `[boot] systemd=true` and that you ran `wsl --shutdown` once after writing it,
  so systemd is PID 1.
