# Running Turing on native Windows

Issue #399's lane: the operator desktop app (NSIS installer) and running
`python -m turing` on native Windows. This is **not** the Surface always-on
host — that remains WSL2 + `bootstrap-surface.ps1` (Slice F, issue #279).
Native Windows here means a real `win32` Python and a real `.exe` desktop
shell, no WSL involved.

## Desktop app: install

The Windows desktop artifact is built by CI (the `desktop-windows` job),
not locally on macOS/Linux:

1. Open the run's **Artifacts** and download `turing-desktop-windows-nsis`.
   It contains `turing_<version>_x64-setup.exe` (NSIS).
2. Run the installer. It installs **per-user** (`installMode:
   "currentUser"`) — no admin prompt, files land under
   `%LOCALAPPDATA%\Programs`, and it registers a normal per-user
   uninstaller.
3. If WebView2 is missing (stock Windows 11 has it; some Server/LTSC images
   don't), the installer downloads the Evergreen WebView2 bootstrapper
   automatically (`webviewInstallMode: "downloadBootstrapper"`, silent).

An MSI can be produced the same way when needed: the CI build selects
bundles per-invocation (`tauri build --bundles nsis`), so `--bundles msi`
(WiX) works without any config change. NSIS is the supported default.

### Defender / SmartScreen (unsigned installer)

The installer is **not code-signed** — `bundle.windows` in
`desktop/src-tauri/tauri.conf.json` carries placeholders
(`certificateThumbprint: null`, `timestampUrl: null`,
`digestAlgorithm: "sha256"`) until a signing certificate exists. Expect:

- **SmartScreen**: "Windows protected your PC" on first run. Click
  **More info → Run anyway**. This is the standard flow for unsigned
  binaries; reputation builds per-file-hash, so every new build warns
  again.
- **Browser/Defender download warning**: "…-setup.exe isn't commonly
  downloaded" — choose *Keep*.
- Defender real-time protection may briefly scan the installer; that is
  normal. If it quarantines it (rare, hash-reputation based), restore it
  from **Windows Security → Protection history** and add an exclusion, or
  verify the build yourself from CI.

When a certificate is acquired, fill in `certificateThumbprint` (and a
`timestampUrl`, e.g. a public RFC-3161 endpoint) and both warnings go away.
Do not put fake values there — a wrong thumbprint fails the build.

### Desktop config location

The config overlay the app reads (never writes) lives at:

| Platform | Path |
|---|---|
| macOS / Linux | `~/.config/turing-desktop/config.json` |
| Windows | `%APPDATA%\turing-desktop\config.json` (Roaming) |

Same JSON shape on every platform — see `desktop/README.md` → Config.
Windows paths in `roots` should use forward slashes or escaped backslashes
in the JSON (`"C:/Users/owen/research-results"` is fine).

## Python runtime: `python -m turing`

Requires Python 3.11+ from python.org (or `winget install Python.Python.3.11`).
From PowerShell, in the repo root:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install hatchling
pip install -e ".[dev]"
python -m turing
```

Environment variables go in `.env` in the working directory exactly as on
POSIX (`TURING_…` prefix), or per-session via
`$env:TURING_ENV = "development"`.

### What is different on Windows (and already handled)

- **Shutdown signals** — the Proactor event loop (the Windows default) has
  no `add_signal_handler`; Turing falls back to `signal.signal` so Ctrl+C
  shuts down gracefully instead of crashing at startup
  (`turing.oscompat.install_signal_handlers`).
- **Shell tool runs PowerShell** — `powershell.exe -NoProfile
  -NonInteractive -Command <cmd>`, not cmd.exe. It is a plain
  pipe-connected subprocess, **not a ConPTY**: there is no pseudo-console,
  so interactive/full-screen programs (editors, TUIs, anything that
  prompts) will hang or misbehave — the tool is for non-interactive
  commands. (The desktop app's built-in terminal panes are a different
  code path: `portable-pty` uses ConPTY on Windows and is fully
  interactive.)
- **No console flashing** — every subprocess Turing spawns on Windows sets
  `CREATE_NO_WINDOW`, so tools don't pop console windows when the runtime
  is started without one.
- **`ping`** — the network tool uses `-n`/`-w` (Windows spelling) instead
  of `-c`/`-W`.
- **Sandbox** — bubblewrap does not exist on Windows. With
  `TURING_SANDBOX_ENABLED=true` the shell tool fails **closed** with a
  clear error; set `TURING_SANDBOX_ENABLED=false` explicitly to run shell
  commands unsandboxed. There is no Windows sandbox equivalent wired up.
- **Service management** — the process tool's `manage_service` action is
  systemd-only and returns a clear error on Windows (see below).

### Paths and data

Runtime data paths are configuration, not platform convention: `db_path`
defaults to `./data/turing.db` **relative to the working directory** on
every platform (including Windows), and `TURING_DB_PATH` /
`TURING_VAULT_ROOT` / `TURING_EMBEDDING_MODEL_PATH` override it. If you
want the data under `%APPDATA%`, say so explicitly:

```powershell
$env:TURING_DB_PATH = "$env:APPDATA\turing\turing.db"
```

The `%APPDATA%` convention is applied automatically only where Turing owns
the location outright: the desktop shell's config file (table above).

## Run at login: Task Scheduler, not a service

Turing deliberately ships **no Windows Service wrapper** (out of scope for
issue #399; a service needs session-0 isolation handling, SCM lifecycle,
and recovery config that the agent does not benefit from). Use Task
Scheduler:

```powershell
# From an elevated-or-not PowerShell; runs in your user session at logon.
$action  = New-ScheduledTaskAction -Execute "$PWD\.venv\Scripts\python.exe" `
           -Argument "-m turing" -WorkingDirectory "$PWD"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"
Register-ScheduledTask -TaskName "Turing" -Action $action -Trigger $trigger
```

Manage it with `Start-ScheduledTask`/`Stop-ScheduledTask -TaskName Turing`
or `taskschd.msc`. For an always-on box that should run without a logged-in
user, use the WSL2 Surface coordinator path (issue #279) instead.

## Known limits on native Windows

- The bubblewrap sandbox and `manage_service` (systemctl) are unavailable —
  both degrade with explicit errors rather than pretending.
- Ollama, NATS, and the mesh all work when their Windows builds are
  installed, but the multi-node docker-compose simulation assumes a
  Linux-container docker engine (Docker Desktop handles this).
- Nothing bundles a Python runtime into the NSIS installer — the desktop
  app is the operator surface only; the agent runtime is the separate
  `python -m turing` install above.
