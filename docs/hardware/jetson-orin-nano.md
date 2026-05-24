# Jetson Orin Nano — Worker Node Setup

End-to-end provisioning for a single NVIDIA Jetson Orin Nano running a Turing
**worker** node. Follow this doc once per Jetson.

The coordinator (Discord bot, Anthropic budget gate, operator UI gateway)
runs on pi-alpha and is **out of scope** here — Jetsons are local-only
workers. Multi-node orchestration (mesh, NATS, peer discovery) is also out
of scope and will live in `orchestration.md` once the new orchestrator design
is settled.

---

## Overview

What you end up with after following this doc:

- A Jetson Orin Nano running Ubuntu 22.04 (JetPack 6), reachable over Tailscale
  by a stable hostname (e.g. `jetson-1`).
- A dedicated unprivileged `turing` Linux user that owns the codebase, venv,
  data, and the running service. Not in the `sudo` group.
- Python 3.11 installed via `uv` (deadsnakes has no aarch64 builds), in a per-user venv.
- Ollama installed with GPU acceleration, `llama3.2:3b` pulled and ready.
- ONNX `all-MiniLM-L6-v2` embedding model fetched for semantic memory.
- The Turing worker running under `systemd`, autostarted on boot, sandboxed
  via `bubblewrap`, mesh disabled — local-only, no Discord, no cloud LLM.

### Prerequisites

Before you start:

- Jetson Orin Nano with JetPack 6 flashed and booted, connected to your LAN.
- SSH access to the Jetson as a sudo-capable user (the user JetPack creates
  during first-boot setup — referred to below as `allen`; substitute your
  own).
- A GitHub account with access to `oap22/Turing`.
- A Tailscale account (free tier is fine).

### Quick start (automated)

A script automates every phase below. SSH into the Jetson as the
sudo-capable user, clone the repo to a scratch location, and run it:

```bash
git clone https://github.com/oap22/Turing.git /tmp/turing-bootstrap
bash /tmp/turing-bootstrap/scripts/setup-jetson.sh
```

It will prompt for the hostname (e.g. `jetson-1`) and hand off to
Tailscale and `gh auth login` for their interactive auth flows. Everything
else — packages, `uv`, Ollama, `jtop`, the `turing` user, clone, venv,
embedding model, `.env`, systemd — runs unattended and is idempotent
(re-run safely).

The rest of this doc is the manual walkthrough — read it once to
understand what the script does, or follow it step-by-step if you'd
rather not run a script blind.

### Phases

1. System provisioning (as `allen`, with sudo)
2. Create the `turing` user
3. Deploy the code (as `turing`)
4. Configure `.env`
5. Install & start the systemd service
6. Verify the worker came up cleanly
7. Operating the node

Plus one appendix: troubleshooting.

---

## Phase 1 — System provisioning

Run everything in this phase as your sudo-capable user (`allen`).

### 1.1 Set the hostname

```bash
sudo hostnamectl set-hostname jetson-1
```

Increment the number for your second and third Jetsons.

### 1.2 Install Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the printed URL to authenticate the device into your tailnet. After
this, your Mac and any other Tailscale-connected device can reach the Jetson
at `jetson-1` (Tailscale's MagicDNS) regardless of physical network.

### 1.3 System updates and build tools

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y build-essential libsqlite3-dev bubblewrap curl git zstd
```

`bubblewrap` is required for the agent's tool sandbox.

### 1.4 Python 3.11 via `uv`

JetPack 6's stock Python is 3.10; Turing requires 3.11. The deadsnakes PPA
does **not** publish `python3.11` for arm64/aarch64, so `apt install
python3.11` fails on Jetson with "Unable to locate package". Use `uv`
instead — it installs a prebuilt standalone Python 3.11 for aarch64.

Install `uv` system-wide so both `allen` and `turing` can use it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
```

Verify:

```bash
uv --version
```

The actual Python 3.11 install happens per-user in Phase 3.2 (uv caches
it under `~/.local/share/uv/python`).

### 1.5 Ollama (GPU-accelerated)

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

The installer extracts its tarball with `zstd`, so `zstd` must already be
installed (covered in 1.3 above) — otherwise the script fails with
"requires zstd for extraction" on Debian/Ubuntu/JetPack.

The installer detects the Tegra/CUDA stack on Jetson and installs the GPU
runtime automatically. It also creates and starts a systemd service for
Ollama (`ollama.service`).

Verify Ollama is up:

```bash
systemctl status ollama         # should be active (running)
ollama --version
```

### 1.6 jtop (Jetson telemetry)

`jtop` (from `jetson-stats`) is the standard interactive monitor for the
Jetson — CPU/GPU/memory/thermals/power in one TUI. Handy for confirming
GPU acceleration during model runs and for ongoing health checks.

```bash
sudo apt install -y python3-pip
sudo pip3 install -U jetson-stats
sudo systemctl restart jtop.service
```

You may need to log out and back in once for group membership to take
effect. Then run `jtop` from any shell.

---

## Phase 2 — Create the `turing` user

Still as `allen`.

### 2.1 Create the user (no sudo group)

```bash
sudo adduser turing
```

Accept the defaults; set a password you'll remember. **Do not** add `turing`
to the `sudo` group — the agent must run unprivileged.

### 2.2 Drop into a `turing` shell

```bash
sudo su - turing
```

You do not need to log out of your `allen` session. The rest of this doc up
through Phase 4 runs inside this `turing` shell.

### 2.3 SSH deploy key for GitHub

```bash
ssh-keygen -t ed25519 -C "jetson-1-turing" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

Copy the printed public key. On GitHub:

1. Go to https://github.com/settings/keys
2. Click **New SSH key**
3. Title: `jetson-1-turing` (so you can revoke per-device later)
4. Paste the key, save

Test the connection:

```bash
ssh -T git@github.com
# Expected: "Hi oap22! You've successfully authenticated..."
```

---

## Phase 3 — Deploy the code

As `turing`.

### 3.1 Clone the repo

```bash
git clone git@github.com:oap22/Turing.git ~/turing
cd ~/turing
```

### 3.2 Create the venv and install Turing

```bash
uv python install 3.11
uv venv --python 3.11 --seed ~/venv
uv pip install --python ~/venv/bin/python --upgrade pip wheel
uv pip install --python ~/venv/bin/python hatchling
uv pip install --python ~/venv/bin/python -e ~/turing
```

`uv python install 3.11` downloads a prebuilt aarch64 Python 3.11 into
`~/.local/share/uv/python` (no compilation). `uv venv` then creates a
standard venv at `~/venv` whose `python` and `pip` work exactly like a
`python3.11 -m venv`-created one.

This takes several minutes — `onnxruntime`, `numpy`, `sentence-transformers`
dependencies, etc. all compile or download for aarch64.

### 3.3 Fetch the embedding model

The agent uses `all-MiniLM-L6-v2` (ONNX form) for semantic memory search.
There is no fetch script in the repo today — do it manually.

Pull the two files directly with `curl` — avoids the moving target of
`huggingface-cli` / `hf` CLI flag changes, and lands them exactly where
the agent expects them.

```bash
mkdir -p ~/turing/models/all-MiniLM-L6-v2
curl -L -o ~/turing/models/all-MiniLM-L6-v2/model.onnx \
    https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx
curl -L -o ~/turing/models/all-MiniLM-L6-v2/tokenizer.json \
    https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/tokenizer.json
```

Verify both files exist and are non-trivial in size (model.onnx is ~90 MB):

```bash
ls -lh ~/turing/models/all-MiniLM-L6-v2/
# Expected: model.onnx (~90M)  tokenizer.json (~700K)
```

### 3.4 Pull the local LLM

```bash
ollama pull llama3.2:3b
```

About 2 GB. Verify it loads on the GPU:

```bash
ollama run llama3.2:3b "say hi"
# Watch `nvidia-smi` or `tegrastats` in another terminal —
# GPU utilisation should spike during the response.
```

### 3.5 Create the data directory

```bash
mkdir -p ~/turing/data
```

---

## Phase 4 — Configure `.env`

As `turing`, in `~/turing`.

```bash
cd ~/turing
cp .env.example .env
sudo apt install -y nano   # if not already installed
nano .env
```

The `.env` has two categories of fields for a Jetson worker. Everything
coordinator-related (Discord, Anthropic, gateway) stays empty — those live
on pi-alpha.

#### A. Per-device — set fresh on every Jetson

```bash
TURING_NODE_NAME=jetson-1   # must be unique across the fleet (jetson-1, jetson-2, …)
```

#### B. Shared — copy the same value to every Jetson

```bash
TURING_OLLAMA_MODEL=llama3.2:3b
TURING_EMBEDDING_MODEL_PATH=/home/turing/turing/models/all-MiniLM-L6-v2
TURING_DB_PATH=/home/turing/turing/data/turing.db
TURING_ALLOWED_WRITE_PATHS=["/tmp","/home/turing/turing/data"]
TURING_SANDBOX_ENABLED=true
TURING_LLM_ROUTING_MODE=local   # workers never call the cloud directly
TURING_MESH_ENABLED=false       # leave false until the new orchestrator ships
TURING_GATEWAY_ENABLED=false    # operator UI lives on pi-alpha only
TURING_ENV=production
```

> **Leave these empty on Jetson workers:** `TURING_DISCORD_TOKEN`,
> `TURING_ANTHROPIC_API_KEY`, `TURING_GATEWAY_TOKEN`. They belong on
> pi-alpha (the coordinator). Putting a Discord token on a worker fights
> the coordinator's bot session; putting an Anthropic key on a worker
> bypasses the single budget gate.

Save and lock down permissions:

```bash
chmod 600 ~/turing/.env
```

> **Mesh is off intentionally.** Inter-node orchestration is being redesigned.
> Leave `TURING_MESH_ENABLED=false` on every node until the new orchestrator
> ships.

---

## Phase 5 — systemd service

Back to your `allen` shell (`exit` the `turing` shell, or open a new SSH).

### 5.1 Install the unit

```bash
sudo tee /etc/systemd/system/turing.service > /dev/null <<'SERVICE'
[Unit]
Description=Turing AI Assistant
After=network.target ollama.service
Requires=ollama.service

[Service]
Type=simple
User=turing
WorkingDirectory=/home/turing/turing
ExecStart=/home/turing/venv/bin/python -m turing
Restart=always
RestartSec=10
EnvironmentFile=/home/turing/turing/.env

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable turing
sudo systemctl start turing
```

### 5.2 Confirm it came up cleanly

```bash
sudo systemctl status turing
sudo journalctl -u turing -f
```

In the logs you should see (roughly, JSON-formatted): config loaded, memory
store initialised, embedding model loaded, Ollama provider ready, tools
registered, sandbox enabled, **mesh disabled**, **Discord disabled**.

If you see `Vector search disabled` — Phase 3.3 didn't land the files in the
right place. See Appendix A.

---

## Phase 6 — Verify the worker came up cleanly

A Jetson worker has no user-facing surface of its own — verification is
log-based plus a reboot test.

1. `sudo systemctl status turing` shows `active (running)` with no recent
   restarts.
2. `sudo journalctl -u turing -n 200 --no-pager` shows a clean startup:
   config loaded, embedding model loaded, Ollama reachable, tool registry
   populated, no tracebacks.
3. Confirm Ollama is using the GPU:
   ```bash
   ollama run llama3.2:3b "say hi"
   # In another terminal: tegrastats — GPU utilisation should spike.
   ```
4. Reboot the Jetson (`sudo reboot`). After it comes back, repeat step 1.
   Confirms `systemctl enable` worked and Ollama starts before Turing.

If all four pass, the worker is provisioned. End-to-end task verification
happens from pi-alpha once orchestration is wired up.

---

## Phase 7 — Operating the node

Common commands, all from your `allen` shell.

| Task | Command |
|---|---|
| Status | `sudo systemctl status turing` |
| Logs (follow) | `sudo journalctl -u turing -f` |
| Restart | `sudo systemctl restart turing` |
| Stop / start | `sudo systemctl stop turing` / `start` |
| Update code | `sudo -u turing bash -c 'cd ~/turing && git pull && uv pip install --python ~/venv/bin/python -e .'` then `sudo systemctl restart turing` |
| Edit config | `sudo -u turing nano /home/turing/turing/.env` then restart |
| Swap Ollama model | `sudo -u turing ollama pull <name>`, edit `.env`, restart |
| Check GPU use | `tegrastats` or `nvidia-smi` |

### Repeating this doc for additional Jetsons

Every step works unchanged. The only per-device differences:

- Hostname in 1.1 (`jetson-2`, `jetson-3`, …)
- SSH key comment in 2.3 (`jetson-2-turing`, etc.) — and add each key
  separately to GitHub so you can revoke per-device
- `TURING_NODE_NAME` in Phase 4

---

## Appendix A — Troubleshooting

### `Vector search disabled` in logs

The embedding model files aren't where the loader expects. Check:

```bash
ls /home/turing/turing/models/all-MiniLM-L6-v2/
# Must contain BOTH: model.onnx  tokenizer.json
```

If `model.onnx` is inside an `onnx/` subdirectory, move it up one level (see
Phase 3.3). If both files exist, confirm `TURING_EMBEDDING_MODEL_PATH` in
`.env` matches the absolute path.

### Ollama is using CPU, not GPU

```bash
journalctl -u ollama -n 50 | grep -iE "gpu|cuda|tegra"
```

You should see CUDA/Tegra detection lines. If not, reinstall Ollama after
confirming JetPack's CUDA stack is intact (`nvcc --version`).

### Service crashes immediately on start

```bash
sudo journalctl -u turing -n 100 --no-pager
```

Most common causes:
- `.env` missing or unreadable by `turing` → `ls -la /home/turing/turing/.env`
- Ollama not running → `systemctl status ollama`
- Embedding model files in the wrong location (see the first entry above)

### Sandbox (`bubblewrap`) failures

If tool execution fails with `bwrap: ...`:

```bash
which bwrap                       # /usr/bin/bwrap
bwrap --bind / / true             # should exit 0
```

On some hardened kernels `bwrap` needs `kernel.unprivileged_userns_clone=1`.
JetPack's default kernel is fine; only an issue if you've customised sysctl.

### Tailscale name doesn't resolve

```bash
tailscale status                  # confirm the device is up
tailscale ip -4                   # confirm an IP is assigned
```

If MagicDNS isn't resolving, enable it in the Tailscale admin console
(**DNS → MagicDNS: enabled**).
