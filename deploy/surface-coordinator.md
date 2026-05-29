# Coordinator deploy — Surface Pro + WSL2 Ubuntu (ADR 0009, issue #258)

The always-on **coordinator** runs on the **Surface Pro (16 GB, Windows)** inside
**WSL2 Ubuntu**. It owns orchestration, the scheduler, the Discord bot, the vault
index, the SQLite episode store, the budget gate, and the webui — and runs **no
local LLM inference**, so 16 GB is ample.

> **Why WSL2, not native Windows.** The entire safety model is Linux-shaped: the
> shell safety gate, the `rm -rf /`-class deny-list, `asyncio.create_subprocess_shell`,
> and the `systemd` units (`turing-trainer`). Native Windows would force a rewrite
> of the most security-critical code. WSL2 keeps Linux semantics intact while the
> Surface provides the always-on host. (ADR 0009 §1.)

This doc is the operator runbook. The hardware-verification steps (a Jetson on the
LAN actually reaching the coordinator, units actually starting on boot) must be
confirmed on the real Surface — the config files and scripts here are what you
apply to get there.

---

## 1. Windows host — power settings (do not skip)

A coordinator that sleeps takes the whole fleet's brain offline. Set: **never
sleep**, **do nothing on lid close**, **always plugged in**.

Run [`scripts/surface-power.ps1`](scripts/surface-power.ps1) from an **elevated
PowerShell**, or apply manually:

```powershell
# Never sleep (AC and DC)
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0

# "Do nothing" on lid close (LIDACTION = 0) for the active scheme
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
```

Keep the Surface **plugged in**. (Optional: disable fast-startup so a reboot is a
clean boot.) Verify with `powercfg /q SCHEME_CURRENT SUB_BUTTONS`.

## 2. WSL2 — systemd

The `systemd` units (`turing-trainer`, and the coordinator service unit) need
`systemd` as PID 1 inside the distro. Put [`wsl/wsl.conf`](wsl/wsl.conf) at
`/etc/wsl.conf` **inside** the Ubuntu distro:

```ini
[boot]
systemd=true
```

Then from Windows: `wsl --shutdown` and restart the distro. Verify inside WSL:

```bash
systemctl is-system-running   # "running" or "degraded" (not "offline")
ps -p 1 -o comm=              # systemd
```

Install the coordinator unit ([`turing-coordinator.service`](turing-coordinator.service)):

```bash
sudo cp deploy/turing-coordinator.service /etc/systemd/system/
# Create /etc/turing/coordinator.env with the coordinator's settings
# (Discord/Anthropic keys, TURING_GATEWAY_ENABLED=true, NATS bind, $TURING_DB_PATH).
# The coordinator runs NO local inference — do not set a worker model here.
sudo systemctl daemon-reload && sudo systemctl enable --now turing-coordinator
```

## 3. WSL2 — networking (the NAT gotcha)

**WSL2 is NAT'd by default** — the Jetsons on the LAN cannot reach services bound
inside the distro. Pick one:

### Preferred: mirrored networking (Windows 11)

Put [`wsl/.wslconfig`](wsl/.wslconfig) at `C:\Users\<you>\.wslconfig` on the
**Windows** side:

```ini
[wsl2]
networkingMode=mirrored

[experimental]
hostAddressLoopback=true
```

`wsl --shutdown`, restart. In mirrored mode the distro shares the host's LAN
addresses, so the coordinator's **NATS (4222)** and **gateway (8765)** are reachable
at the Surface's LAN IP directly.

### Fallback: port-proxy (Windows 10 / mirrored unavailable)

Run [`scripts/wsl-portproxy.ps1`](scripts/wsl-portproxy.ps1) (elevated). It forwards
the Windows LAN interface → the WSL2 internal IP for ports 4222 and 8765 and opens
the matching firewall rules. Re-run after a reboot if the WSL2 IP changed (the
script reads it fresh from `wsl hostname -I`).

> **Gotcha:** with the default NAT mode and no proxy, `nats://<surface-lan-ip>:4222`
> times out from a Jetson even though it works from inside WSL. If a worker can't
> register, check this first.

## 4. No local LLM inference

The coordinator runs orchestration + diagnostics only. Keep `TURING_LLM_ROUTING_MODE`
off `local` for any coordinator-resident agent path, and do **not** run Ollama on the
Surface — inference belongs on the Jetsons (see [`jetson-worker.md`](jetson-worker.md)).

## 5. Acceptance checklist

| Acceptance criterion (#258) | How to confirm | Owner |
|---|---|---|
| Coordinator process group runs under WSL2 with `systemd` active (units start on boot) | `systemctl is-system-running`; reboot, confirm units up | operator (hardware) |
| Windows power: no sleep, no-op on lid close, always plugged in | `powercfg /q` after running `surface-power.ps1` | operator (hardware) |
| A Jetson on the LAN can reach NATS (4222) + gateway (8765) | from a Jetson: `nc -vz <surface-lan-ip> 4222` and `4222`/`8765` | operator (hardware) |
| No local LLM inference on the coordinator | no Ollama process; routing not `local` | operator |
| Deploy doc records power, `systemd=true`, networking + the NAT gotcha | this document | ✅ done |

**Fallback if lid-sleep reliability fails:** promote one Jetson to coordinator
(3 workers). Documented as the ADR 0009 fallback; revisit only if needed.
