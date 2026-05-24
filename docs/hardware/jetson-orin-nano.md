# Jetson Orin Nano — Hardware Setup

End-to-end provisioning for a single NVIDIA Jetson Orin Nano running a Turing
node. Follow this doc once per device. Multi-node orchestration (mesh, NATS,
peer discovery) is intentionally **out of scope** and will live in a separate
`orchestration.md` once the new orchestrator design is settled.

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
- The Turing agent running under `systemd`, autostarted on boot, sandboxed
  via `bubblewrap`, mesh disabled, with a transitional Discord bot wired up
  so you can verify the loop end-to-end.

### Prerequisites

Before you start:

- Jetson Orin Nano with JetPack 6 flashed and booted, connected to your LAN.
- SSH access to the Jetson as a sudo-capable user (the user JetPack creates
  during first-boot setup — referred to below as `allen`; substitute your
  own).
- A GitHub account with access to `oap22/Turing`.
- An Anthropic API key (https://console.anthropic.com).
- A Discord account (you'll create the bot in Appendix A — *transitional*).
- A Tailscale account (free tier is fine).

### Phases

1. System provisioning (as `allen`, with sudo)
2. Create the `turing` user
3. Deploy the code (as `turing`)
4. Configure `.env`
5. Install & start the systemd service
6. End-to-end verification with Discord
7. Operating the node

Plus two appendices: creating the Discord bot, and troubleshooting.

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

```bash
uv pip install --python ~/venv/bin/python "huggingface_hub[cli]"
mkdir -p ~/turing/models/all-MiniLM-L6-v2
~/venv/bin/huggingface-cli download Xenova/all-MiniLM-L6-v2 \
    onnx/model.onnx tokenizer.json \
    --local-dir ~/turing/models/all-MiniLM-L6-v2
# Move model.onnx out of the onnx/ subdir into the model dir root
mv ~/turing/models/all-MiniLM-L6-v2/onnx/model.onnx \
   ~/turing/models/all-MiniLM-L6-v2/model.onnx
```

Verify both files exist:

```bash
ls ~/turing/models/all-MiniLM-L6-v2/
# Expected: model.onnx  tokenizer.json  onnx/
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
cp .env.example .env
nano .env
```

Set these fields (everything else can keep its default):

```bash
TURING_NODE_NAME=jetson-1
TURING_DISCORD_TOKEN=<paste from Appendix A>
TURING_DISCORD_ADMIN_IDS=[<your Discord numeric user ID>]
TURING_ANTHROPIC_API_KEY=sk-ant-...
TURING_OLLAMA_MODEL=llama3.2:3b
TURING_EMBEDDING_MODEL_PATH=/home/turing/turing/models/all-MiniLM-L6-v2
TURING_DB_PATH=/home/turing/turing/data/turing.db
TURING_ALLOWED_WRITE_PATHS=["/tmp","/home/turing/turing/data"]
TURING_SANDBOX_ENABLED=true
TURING_MESH_ENABLED=false
TURING_ENV=production
```

Save and lock down permissions — this file holds two API tokens:

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
store initialised, embedding model loaded, both LLM providers ready, tools
registered, sandbox enabled, **mesh disabled**, Discord bot connected.

If you see `Vector search disabled` — Phase 3.3 didn't land the files in the
right place. See Appendix B.

---

## Phase 6 — End-to-end verification (Discord)

> Discord is the verification channel **only because the native operator
> interface doesn't have a chat input yet**. Once it does, delete the bot
> application and remove `TURING_DISCORD_TOKEN` from `.env`.

1. Confirm the bot shows online in your test Discord server.
2. In any channel the bot can see, send:
   ```
   !turing hello
   ```
3. Watch `journalctl -u turing -f` — you should see the message arrive,
   context build (4 parallel retrievals), an LLM call (cloud, since tools
   are present), and a reply written back to Discord.
4. Reboot the Jetson (`sudo reboot`) and repeat step 2 once it's back.
   Confirms `systemd enable` worked and Ollama comes up before Turing.

If all three pass, the node is provisioned.

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

### Repeating this doc for jetsons 2 and 3

Every step works unchanged. The only per-device differences:

- Hostname in 1.1 (`jetson-2`, `jetson-3`)
- SSH key comment in 2.3 (`jetson-2-turing`, etc.) — and add each key
  separately to GitHub so you can revoke per-device
- `TURING_NODE_NAME` in Phase 4

You do **not** need to create a second Discord bot — one bot, all three
nodes. (When orchestration lands, only one node will actually own the bot
session.)

---

## Appendix A — Creating the Discord bot (transitional)

Skip if you already have a bot token. **Delete this appendix once the native
operator interface ships.**

### A.1 Create the application

1. Go to https://discord.com/developers/applications
2. **New Application** → name it `Turing` (or anything; only you see this)
3. Left sidebar → **Bot**
4. Under **Privileged Gateway Intents**, enable:
   - **MESSAGE CONTENT INTENT** (required — without this the bot sees empty
     message bodies)
   - **SERVER MEMBERS INTENT** (optional, useful later)
5. Under the bot username, click **Reset Token** → **Yes** → copy the token.
   This is your `TURING_DISCORD_TOKEN`. **You can only see it once.**

### A.2 Invite the bot to a server

1. Left sidebar → **OAuth2** → **URL Generator**
2. Scopes: check **bot**
3. Bot Permissions: check **Send Messages**, **Read Message History**,
   **View Channels**
4. Copy the generated URL at the bottom, open it in a browser, select a
   server you own (create a private test server if you don't have one),
   authorise.

### A.3 Get your Discord user ID

1. In Discord, **Settings → Advanced → Developer Mode: ON**
2. Right-click your own name anywhere → **Copy User ID**
3. That numeric string is your `TURING_DISCORD_ADMIN_IDS` entry. Wrap it in
   the JSON list: `TURING_DISCORD_ADMIN_IDS=[123456789012345678]`.

---

## Appendix B — Troubleshooting

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
- Missing API key → grep for `Anthropic` in the error
- Ollama not running → `systemctl status ollama`
- Port collision on the embedded webui gateway (only matters if you flip
  `TURING_GATEWAY_ENABLED=true`)

### Sandbox (`bubblewrap`) failures

If tool execution fails with `bwrap: ...`:

```bash
which bwrap                       # /usr/bin/bwrap
bwrap --bind / / true             # should exit 0
```

On some hardened kernels `bwrap` needs `kernel.unprivileged_userns_clone=1`.
JetPack's default kernel is fine; only an issue if you've customised sysctl.

### Discord bot is online but doesn't reply

Almost always the **MESSAGE CONTENT INTENT** was not enabled in the Discord
Developer Portal (Appendix A.1, step 4). The bot sees an empty `content`
string, so the prefix doesn't match. Toggle it on, no code change needed.

### Tailscale name doesn't resolve

```bash
tailscale status                  # confirm the device is up
tailscale ip -4                   # confirm an IP is assigned
```

If MagicDNS isn't resolving, enable it in the Tailscale admin console
(**DNS → MagicDNS: enabled**).
