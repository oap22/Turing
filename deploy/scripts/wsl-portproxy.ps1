<#
.SYNOPSIS
  Fallback for WSL2 NAT (issue #258): forward the Surface's LAN interface to the
  WSL2 internal IP for the coordinator's NATS (4222) and gateway (8765) ports,
  and open the matching firewall rules. Use this only when mirrored networking
  (.wslconfig) is unavailable (e.g. Windows 10).

.DESCRIPTION
  Run from an ELEVATED PowerShell. WSL2's internal IP changes across reboots, so
  re-run this after a reboot — it reads the current IP fresh from `wsl hostname -I`.
#>

$ErrorActionPreference = "Stop"

# Ports the Jetsons must reach on the coordinator.
$ports = @(4222, 8765)

# Current WSL2 internal IP (first address from `hostname -I`).
$wslIp = (wsl hostname -I).Trim().Split(" ")[0]
if (-not $wslIp) { throw "Could not determine WSL2 IP (is the distro running?)" }
Write-Host "WSL2 IP: $wslIp"

foreach ($p in $ports) {
    Write-Host "Forwarding 0.0.0.0:$p -> ${wslIp}:$p"
    netsh interface portproxy delete v4tov4 listenport=$p listenaddress=0.0.0.0 2>$null
    netsh interface portproxy add v4tov4 listenport=$p listenaddress=0.0.0.0 `
        connectport=$p connectaddress=$wslIp

    $rule = "WSL Turing $p"
    netsh advfirewall firewall delete rule name="$rule" 2>$null
    netsh advfirewall firewall add rule name="$rule" dir=in action=allow `
        protocol=TCP localport=$p
}

Write-Host "`nActive portproxy table:"
netsh interface portproxy show v4tov4
