<#
.SYNOPSIS
  Apply the ADR 0009 coordinator power policy to the Surface Pro (issue #258):
  never sleep, do-nothing on lid close, no hibernate. Keep it plugged in.

.DESCRIPTION
  Run from an ELEVATED PowerShell. Idempotent: re-running just re-asserts the
  settings. A coordinator that sleeps takes the whole fleet's brain offline.

.NOTES
  SUB_BUTTONS GUID 4f971e89-eebd-4455-a8de-9e59040e7347
  LIDACTION   GUID 5ca83367-6e45-459f-a27b-476b1d01c936  (0 = Do nothing)
#>

$ErrorActionPreference = "Stop"

Write-Host "Disabling sleep + hibernate timeouts (AC and DC)..."
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0

Write-Host "Setting 'Do nothing' on lid close for the active scheme..."
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT

Write-Host "Done. Current button/lid settings:"
powercfg /q SCHEME_CURRENT SUB_BUTTONS

Write-Host "`nReminder: keep the Surface plugged in (AC). Optionally disable"
Write-Host "fast-startup so a reboot is a clean boot."
