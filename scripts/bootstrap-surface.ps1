<#
.SYNOPSIS
  Windows-side bootstrap for the Turing Surface/WSL2 coordinator (ADR 0010 §5,
  issue #279). Idempotent: every step is check-first, so re-runs are clean
  no-ops. Brings the Windows host to the point where `setup-coordinator.sh`
  (Slice G) can run inside a known-good WSL2 Ubuntu.

.DESCRIPTION
  Run from an ELEVATED PowerShell (most steps need admin). This script:

    1. Enables WSL2 + Virtual Machine Platform Windows features.
    2. Installs the Ubuntu distro (no-op if already present).
    3. Writes C:\Users\<you>\.wslconfig with networkingMode=mirrored.
    4. Writes /etc/wsl.conf inside the distro with [boot] systemd=true.
    5. Sets the Windows power plan to never sleep / do-nothing-on-lid on AC.
    6. Opens Windows Firewall inbound for NATS (4222), gateway (8765), and
       ntfy (8090) scoped to the local Tailnet subnet (100.64.0.0/10).
    7. Registers a Task Scheduler entry that runs `wsl -d Ubuntu` at boot and
       restarts it if it exits (always-on posture, ADR 0010 §7).

  Mirrored networking is a Windows 11 22H2+ feature. On a host that lacks clean
  mirrored support, the firewall rules still apply but the coordinator's ports
  will not be reachable from the Tailnet until you run the NAT fallback,
  deploy/scripts/wsl-portproxy.ps1 — see deploy/bootstrap-surface.md §Fallback.

  This script does NOT install Tailscale, NATS, or ntfy on the Windows host —
  those live INSIDE WSL2 (ADR 0010 §8) and are handled by setup-coordinator.sh.

.NOTES
  Ports (all served inside WSL2, Tailnet-facing):
    NATS    4222/tcp
    gateway 8765/tcp
    ntfy    8090/tcp
  Tailnet subnet: 100.64.0.0/10 (Tailscale CGNAT range; ADR 0010 §8).
  Power GUIDs:
    SUB_BUTTONS 4f971e89-eebd-4455-a8de-9e59040e7347
    LIDACTION   5ca83367-6e45-459f-a27b-476b1d01c936  (0 = Do nothing)
  Task Scheduler: 'Turing WSL2 Boot', runs at system startup, restart-on-exit.
#>

[CmdletBinding()]
param(
    # WSL2 distro name. Must match what setup-coordinator.sh expects.
    [string]$DistroName = "Ubuntu",
    # Tailnet subnet the coordinator's inbound ports are scoped to. The default
    # is the Tailscale CGNAT range; override if your tailnet uses a custom CIDR.
    [string]$TailnetSubnet = "100.64.0.0/10",
    # Task Scheduler entry name for the boot-time WSL2 keep-alive.
    [string]$TaskName = "Turing WSL2 Boot"
)

$ErrorActionPreference = "Stop"

# ── helpers ──────────────────────────────────────────────────────────
function Write-Step  { param($Msg) Write-Host "`n==> $Msg" -ForegroundColor Cyan }
function Write-Skip  { param($Msg) Write-Host "    skip: $Msg" -ForegroundColor DarkGray }
function Write-Did   { param($Msg) Write-Host "    done: $Msg" -ForegroundColor Green }
function Write-Note  { param($Msg) Write-Host "    note: $Msg" -ForegroundColor Yellow }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

# Canonicalize a single IPv4 firewall RemoteAddress so re-runs compare equal
# regardless of how Windows stores/reads it back. Get-NetFirewallAddressFilter
# normalizes an IPv4 CIDR (e.g. "100.64.0.0/10") into a start-end range
# ("100.64.0.0-100.127.255.255"), so a string compare against the raw CIDR
# would always miss and trigger a needless delete+recreate. We reduce both the
# stored value and the configured subnet to a "start-end" pair of 32-bit ints
# and compare those. A bare CIDR, an explicit range, and a single IP all reduce
# to the same canonical form. Non-IPv4 / keyword scopes (Any, LocalSubnet, IPv6)
# fall through unchanged so they still compare by string.
function Get-CanonicalAddress {
    param([string]$Address)
    $a = $Address.Trim()
    # All arithmetic is done in [long] (int64) and truncated to 32 bits with
    # `-band 0xFFFFFFFF`; Windows PowerShell 5.1's shift operators do not accept
    # [uint32], so we stay in signed-wide types and mask down at the end.
    $u32 = [long]0xFFFFFFFF
    # IPv4 dotted-quad -> 32-bit value (big-endian, network order).
    $toInt = {
        param($dotted)
        $bytes = [System.Net.IPAddress]::Parse($dotted).GetAddressBytes()
        [array]::Reverse($bytes)
        [System.BitConverter]::ToUInt32($bytes, 0)
    }
    # Explicit range: "start-end".
    if ($a -match '^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$') {
        try {
            return "$(& $toInt $matches[1])-$(& $toInt $matches[2])"
        } catch { return $a }
    }
    # CIDR: "a.b.c.d/len".
    if ($a -match '^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*/\s*(\d{1,2})\s*$') {
        try {
            $ip  = [long](& $toInt $matches[1])
            $len = [int]$matches[2]
            if ($len -lt 0 -or $len -gt 32) { return $a }
            $mask = if ($len -eq 0) { [long]0 } else { ([long]([long]$u32 -shl (32 - $len))) -band $u32 }
            $netStart = $ip -band $mask
            $netEnd   = ($netStart -bor (-bnot $mask)) -band $u32
            return "$netStart-$netEnd"
        } catch { return $a }
    }
    # Single IPv4 host -> degenerate range start==end.
    if ($a -match '^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$') {
        try {
            $ip = [long](& $toInt $matches[1])
            return "$ip-$ip"
        } catch { return $a }
    }
    # Keyword / IPv6 / anything else: compare verbatim.
    return $a
}

# Port map: name -> @(protocol, ports[]). ntfy listens on :8090 (the
# self-hosted server's listen-http, scripts/coordinator/server.yml) — NOT 80/443.
$PortMap = [ordered]@{
    "NATS"    = @{ Protocol = "TCP"; Ports = @(4222) }
    "gateway" = @{ Protocol = "TCP"; Ports = @(8765) }
    "ntfy"    = @{ Protocol = "TCP"; Ports = @(8090) }
}

# ── preflight ────────────────────────────────────────────────────────
if (-not (Test-Admin)) {
    throw "Run this from an ELEVATED PowerShell — feature enable, firewall, and Task Scheduler all need admin."
}

Write-Step "Turing Surface coordinator bootstrap (ADR 0010 §5)"
$winVer   = [System.Environment]::OSVersion.Version
$buildNum = $winVer.Build
Write-Host "    Windows build: $buildNum"
# Mirrored networking needs Windows 11 22H2 (build 22621) or newer. We do not
# refuse on older builds — the rest of the script is still useful — but we warn
# and point at the NAT fallback so the operator is not surprised by unreachable
# coordinator ports. (ADR 0010 §5 consequences: fall back to NAT + port-proxy.)
$MirroredSupported = ($buildNum -ge 22621)
if (-not $MirroredSupported) {
    Write-Note "Build $buildNum predates Windows 11 22H2 (22621) — mirrored networking"
    Write-Note "may not work cleanly. The .wslconfig is still written, but if the"
    Write-Note "Jetsons cannot reach the coordinator after first boot, use the NAT"
    Write-Note "fallback: deploy/scripts/wsl-portproxy.ps1 (see deploy/bootstrap-surface.md)."
}

# ── 1. Windows features: WSL + Virtual Machine Platform ──────────────
Write-Step "1. Windows features (WSL2 + Virtual Machine Platform)"
$RebootNeeded = $false
$features = @(
    "Microsoft-Windows-Subsystem-Linux",
    "VirtualMachinePlatform"
)
foreach ($feat in $features) {
    $state = (Get-WindowsOptionalFeature -Online -FeatureName $feat).State
    if ($state -eq "Enabled") {
        Write-Skip "$feat already enabled"
    } else {
        Write-Host "    enabling $feat ..."
        $result = Enable-WindowsOptionalFeature -Online -FeatureName $feat -NoRestart -All
        if ($result.RestartNeeded) { $RebootNeeded = $true }
        Write-Did "$feat enabled"
    }
}
if ($RebootNeeded) {
    Write-Note "A reboot is required to finish enabling WSL2 features. Reboot, then"
    Write-Note "re-run this script — it will pick up where it left off."
}

# ── 2. WSL2 default version + Ubuntu distro ──────────────────────────
Write-Step "2. WSL2 default version + $DistroName distro"
# `wsl --set-default-version 2` is safe to re-run.
& wsl.exe --set-default-version 2 2>$null | Out-Null

# wsl --list --quiet returns installed distro names. Older builds may emit
# UTF-16; normalize before matching. Wrapped in a helper so we can re-probe
# after the install below — capturing $installed once would go stale the moment
# step 2 installs the distro, making step 4 wrongly skip /etc/wsl.conf.
function Get-InstalledDistros {
    try {
        $raw = & wsl.exe --list --quiet 2>$null
        return @($raw | ForEach-Object { ($_ -replace "`0", "").Trim() } | Where-Object { $_ })
    } catch {
        return @()
    }
}
$installed = Get-InstalledDistros
if ($installed -contains $DistroName) {
    Write-Skip "$DistroName already installed"
} else {
    if ($RebootNeeded) {
        Write-Note "Skipping $DistroName install until features are active (reboot first)."
    } else {
        Write-Host "    installing $DistroName (this downloads the rootfs and may take a while)..."
        # --no-launch installs without dropping into the first-run user setup, so
        # the script stays non-interactive. setup-coordinator.sh creates the
        # `turing` service user later; the default WSL user is created on first
        # interactive launch if the operator ever runs `wsl -d Ubuntu` by hand.
        & wsl.exe --install -d $DistroName --no-launch
        Write-Did "$DistroName install requested"
        # Re-probe so step 4 sees the freshly-installed distro and writes
        # /etc/wsl.conf in this same pass — no second run needed when no reboot
        # was required.
        $installed = Get-InstalledDistros
    }
}

# ── 3. .wslconfig (Windows side) — mirrored networking ───────────────
Write-Step "3. .wslconfig — mirrored networking"
$wslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
$wslConfigBody = @"
# .wslconfig — managed by scripts/bootstrap-surface.ps1 (ADR 0010 §5).
# Mirrored networking lets the Jetsons reach the coordinator's NATS (4222),
# gateway (8765), and ntfy (8090) at the Surface's LAN/Tailnet IP, sidestepping
# WSL2's default NAT. After editing: `wsl --shutdown`, then restart the distro.
[wsl2]
networkingMode=mirrored

[experimental]
hostAddressLoopback=true
"@
# Idempotent write: only touch the file if content differs (ignoring CRLF noise).
$writeWslConfig = $true
if (Test-Path $wslConfigPath) {
    $existing = (Get-Content $wslConfigPath -Raw) -replace "`r`n", "`n"
    if ($existing.Trim() -eq $wslConfigBody.Trim()) {
        Write-Skip ".wslconfig already current ($wslConfigPath)"
        $writeWslConfig = $false
    }
}
if ($writeWslConfig) {
    Set-Content -Path $wslConfigPath -Value $wslConfigBody -Encoding ASCII -NoNewline
    Write-Did "wrote $wslConfigPath"
}

# ── 4. /etc/wsl.conf (inside distro) — systemd=true ──────────────────
Write-Step "4. /etc/wsl.conf inside $DistroName — [boot] systemd=true"
if (($installed -contains $DistroName) -and -not $RebootNeeded) {
    # Probe whether systemd is already configured. We grep for the literal
    # `systemd=true` (whitespace-tolerant) under [boot]; if present we skip.
    $hasSystemd = $false
    try {
        $probe = & wsl.exe -d $DistroName -u root -- bash -lc `
            "grep -Eq '^[[:space:]]*systemd[[:space:]]*=[[:space:]]*true' /etc/wsl.conf 2>/dev/null && echo yes || echo no"
        $hasSystemd = ($probe -match "yes")
    } catch {
        $hasSystemd = $false
    }
    if ($hasSystemd) {
        Write-Skip "/etc/wsl.conf already has [boot] systemd=true"
    } else {
        Write-Host "    writing /etc/wsl.conf with [boot] systemd=true ..."
        # Write atomically as root. Single-quoted heredoc so nothing expands.
        & wsl.exe -d $DistroName -u root -- bash -lc @'
set -e
tmp="$(mktemp)"
cat > "$tmp" <<'EOF'
# /etc/wsl.conf — managed by scripts/bootstrap-surface.ps1 (ADR 0010 §5).
# Enables systemd as PID 1 so the turing-coordinator/gateway/vault-watcher
# systemd units run. After editing: `wsl --shutdown` from Windows, restart.
[boot]
systemd=true
EOF
install -m 0644 "$tmp" /etc/wsl.conf
rm -f "$tmp"
'@
        Write-Did 'wrote /etc/wsl.conf (systemd becomes PID 1 after next `wsl --shutdown`)'
        Write-Note "Run 'wsl --shutdown' then restart $DistroName for systemd to take effect."
    }
} else {
    Write-Note "Distro not ready — re-run after $DistroName is installed to write /etc/wsl.conf."
}

# ── 5. Power plan — never sleep on AC ────────────────────────────────
Write-Step "5. Power plan — never sleep, do-nothing on lid (ADR 0010 §7)"
# A coordinator that sleeps takes the whole fleet's brain offline. powercfg is
# inherently idempotent — re-asserting the same value is a no-op. deploy/scripts/
# surface-power.ps1 is the single source of truth for the powercfg invocations
# and the SUB_BUTTONS/LIDACTION GUIDs; dot-source it rather than duplicating that
# logic here so the two never drift. The path is resolved relative to this
# script: scripts/bootstrap-surface.ps1 -> ../deploy/scripts/surface-power.ps1.
$surfacePower = Join-Path $PSScriptRoot "..\deploy\scripts\surface-power.ps1"
if (Test-Path $surfacePower) {
    . $surfacePower
    Write-Did "applied power policy via deploy/scripts/surface-power.ps1"
} else {
    # Fallback if invoked from a context where the repo layout is not intact
    # (e.g. the script was copied standalone). Keep in sync with the canonical
    # deploy/scripts/surface-power.ps1.
    Write-Note "surface-power.ps1 not found next to repo layout; applying power policy inline."
    powercfg /change standby-timeout-ac 0
    powercfg /change standby-timeout-dc 0
    powercfg /change hibernate-timeout-ac 0
    powercfg /change hibernate-timeout-dc 0
    powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
    powercfg /setactive SCHEME_CURRENT
    Write-Did "no sleep/hibernate timeouts; lid-close = do nothing"
}

# ── 6. Firewall — inbound coordinator ports, Tailnet-scoped ──────────
Write-Step "6. Windows Firewall — inbound NATS/gateway/ntfy, scoped to $TailnetSubnet"
foreach ($svc in $PortMap.Keys) {
    $proto = $PortMap[$svc].Protocol
    $ports = $PortMap[$svc].Ports
    foreach ($port in $ports) {
        $ruleName = "Turing Coordinator $svc $port (Tailnet)"
        $existingRule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
        if ($existingRule) {
            # Verify the scope is still correct; if the remote address drifted,
            # re-create the rule so re-runs converge rather than leaving a stale
            # over-broad rule in place. Compare canonicalized forms: Windows reads
            # a stored CIDR back as a start-end range, so a raw string compare
            # against $TailnetSubnet would always miss and force a delete+recreate
            # on every run. Get-CanonicalAddress reduces both sides to the same
            # "start-end" int pair, so a correctly-scoped rule is a true no-op.
            $af = $existingRule | Get-NetFirewallAddressFilter
            $remote = @($af.RemoteAddress)
            $wantCanonical = Get-CanonicalAddress $TailnetSubnet
            $needsFix = -not ($remote.Count -eq 1 -and (Get-CanonicalAddress $remote[0]) -eq $wantCanonical)
            if ($needsFix) {
                Write-Host "    re-scoping $ruleName -> $TailnetSubnet"
                $existingRule | Remove-NetFirewallRule
                $existingRule = $null
            } else {
                Write-Skip "$ruleName already scoped to $TailnetSubnet"
            }
        }
        if (-not $existingRule) {
            New-NetFirewallRule `
                -DisplayName $ruleName `
                -Direction Inbound `
                -Action Allow `
                -Protocol $proto `
                -LocalPort $port `
                -RemoteAddress $TailnetSubnet `
                -Profile Any `
                -Description "Turing coordinator $svc inbound, scoped to the Tailnet subnet (ADR 0010 §5)." `
                | Out-Null
            Write-Did "$ruleName (port $port/$proto from $TailnetSubnet)"
        }
    }
}

# ── 7. Task Scheduler — start WSL2 at boot, restart on exit ──────────
Write-Step "7. Task Scheduler — '$TaskName' (boot start + restart-on-exit)"
# Keep WSL2 alive across reboots and crashes. `wsl -d <distro>` with no command
# starts the distro's init (systemd) and returns; with systemd as PID 1 the
# coordinator's units come up under it. Restart-on-exit covers the case where
# the distro is torn down (e.g. `wsl --shutdown` by Windows Update servicing).
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

# Action: run wsl -d <distro> with no inner command (boots the distro/systemd).
$action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $DistroName"
# Trigger: at system startup (independent of any user logon — autologon is NOT
# used, ADR 0010 §7).
$trigger = New-ScheduledTaskTrigger -AtStartup
# Run as SYSTEM so it fires with no interactive session.
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
# Settings: restart-on-exit semantics. RestartInterval is the minimum allowed
# (1 min); a high RestartCount approximates "always". AllowStartIfOnBatteries +
# DoNotStopIfGoingOnBatteries so a brief unplug never kills the keep-alive.
$settings = New-ScheduledTaskSettingsSet `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

if ($existingTask) {
    # Converge: re-register so the action/trigger/settings match this script's
    # intent even if a prior version registered something slightly different.
    Write-Host "    updating existing task to current definition ..."
    Set-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
    Write-Skip "'$TaskName' already registered (re-asserted definition)"
} else {
    Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
        -Description "Start WSL2 ($DistroName) at boot and restart if it exits — Turing coordinator always-on (ADR 0010 §7)." `
        | Out-Null
    Write-Did "registered '$TaskName' (AtStartup, restart-on-exit, runs wsl -d $DistroName)"
}

# ── summary ──────────────────────────────────────────────────────────
Write-Step "Bootstrap complete"
if ($RebootNeeded) {
    Write-Note "REBOOT REQUIRED to finish enabling WSL2 features, then re-run this script."
    Write-Note "The re-run installs $DistroName and writes /etc/wsl.conf."
} else {
    Write-Host "    Next steps:" -ForegroundColor White
    Write-Host "      1. wsl --shutdown   (so systemd/.wslconfig take effect)" -ForegroundColor White
    Write-Host "      2. wsl -d $DistroName   then run scripts/setup-coordinator.sh (Slice G)" -ForegroundColor White
    Write-Host "      3. Verify: wsl -d $DistroName -- systemctl is-system-running" -ForegroundColor White
    if (-not $MirroredSupported) {
        Write-Note "Mirrored networking is unverified on this build — if Jetsons cannot"
        Write-Note "reach the coordinator, run deploy/scripts/wsl-portproxy.ps1 (NAT fallback)."
    }
}
