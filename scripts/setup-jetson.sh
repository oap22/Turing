#!/usr/bin/env bash
# Fully automated Jetson Orin Nano worker-node setup for Turing.
#
# Run on a freshly flashed JetPack 6 device as the sudo-capable user
# (the account JetPack created on first boot — referred to as `allen`
# in docs/hardware/jetson-orin-nano.md). Idempotent: re-running skips
# steps that already completed.
#
#   bash scripts/setup-jetson.sh
#
# Two interactive hand-offs you cannot skip:
#   1. `tailscale up` prints a URL to authenticate the device
#   2. `gh auth login` (run as the `turing` user) — choose SSH protocol,
#      let gh generate a key, paste the one-time device code in a browser
#
# Everything else is automated.

set -euo pipefail

# ── tunables ─────────────────────────────────────────────────────────
REPO_URL_SSH="git@github.com:oap22/Turing.git"
REPO_URL_HTTPS="https://github.com/oap22/Turing.git"
OLLAMA_MODEL="llama3.2:3b"
EMBEDDING_DIR="/home/turing/turing/models/all-MiniLM-L6-v2"
EMBEDDING_BASE="https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main"

# ── helpers ──────────────────────────────────────────────────────────
log()  { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

require_not_root() {
    [[ $EUID -ne 0 ]] || die "Run as your sudo-capable user, not root. The script will sudo where needed."
}

require_jetson() {
    # This script is for NVIDIA Jetson devices only (Orin Nano, etc.).
    # Refuse to run on a Pi, an x86 box, or anything else.
    local arch model=""
    arch="$(uname -m)"
    [[ "$arch" == "aarch64" ]] || die "Not a Jetson: arch is $arch, expected aarch64."
    [[ -r /proc/device-tree/model ]] || die "Not a Jetson: /proc/device-tree/model missing."
    model="$(tr -d '\0' </proc/device-tree/model)"
    # Jetson devices report e.g. "NVIDIA Orin Nano Developer Kit" or
    # "NVIDIA Jetson AGX Orin". Accept any NVIDIA Tegra/Orin/Xavier/Jetson board.
    [[ "$model" =~ ^NVIDIA && "$model" =~ ([Jj]etson|Orin|Xavier|Tegra) ]] \
        || die "Not a Jetson: device model is '$model'. This script is Jetson-only."
    echo "  detected: $model"
}

confirm() {
    local prompt="$1" reply
    read -r -p "$prompt [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

# Run a command as the `turing` user in a login shell so PATH/HOME are right.
as_turing() { sudo -u turing -H bash -lc "$*"; }

# ── prompts ──────────────────────────────────────────────────────────
require_not_root

log "Verifying this is a Jetson device"
require_jetson

log "Turing Jetson worker setup"
echo "This will provision the current Jetson as a Turing worker node."
echo "Coordinator-only services (Discord, Anthropic, gateway) are NOT installed."

read -r -p "Hostname for this Jetson (e.g. jetson-1): " HOSTNAME_NEW
[[ -n "$HOSTNAME_NEW" ]] || die "Hostname is required."
NODE_NAME="$HOSTNAME_NEW"

# ── Phase 1: system provisioning ─────────────────────────────────────
log "Phase 1.1 — Set hostname to $HOSTNAME_NEW"
if [[ "$(hostnamectl --static)" != "$HOSTNAME_NEW" ]]; then
    sudo hostnamectl set-hostname "$HOSTNAME_NEW"
else
    echo "  already set, skipping"
fi

log "Phase 1.2 — Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
    echo "  authenticating — follow the URL printed below"
    sudo tailscale up
else
    echo "  already up, skipping auth"
fi

log "Phase 1.3 — System packages"
sudo apt-get update
sudo apt-get install -y \
    build-essential libsqlite3-dev bubblewrap curl git zstd \
    nano python3-pip ca-certificates

log "Phase 1.4 — uv (for Python 3.11 on aarch64)"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh \
      | sudo env UV_INSTALL_DIR=/usr/local/bin sh
fi
uv --version

log "Phase 1.5 — Ollama"
if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
fi
sudo systemctl enable --now ollama
ollama --version

log "Phase 1.6 — jtop"
if ! command -v jtop >/dev/null 2>&1; then
    sudo pip3 install -U jetson-stats
    sudo systemctl restart jtop.service || true
fi

log "Phase 1.7 — GitHub CLI (for the turing user's auth in Phase 3)"
if ! command -v gh >/dev/null 2>&1; then
    sudo mkdir -p -m 755 /etc/apt/keyrings
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | sudo dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg
    sudo chmod 644 /etc/apt/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
    sudo apt-get update
    sudo apt-get install -y gh
fi
gh --version | head -n1

# ── Phase 2: turing user ─────────────────────────────────────────────
log "Phase 2.1 — Create the unprivileged turing user"
if ! id turing >/dev/null 2>&1; then
    sudo adduser --disabled-password --gecos "" turing
    echo "  user created (no password set; reach it via sudo). Set one with: sudo passwd turing"
else
    echo "  already exists, skipping"
fi

# ── Phase 3: code deploy as turing ───────────────────────────────────
log "Phase 3.1 — GitHub authentication for the turing user"
if ! sudo -u turing -H gh auth status >/dev/null 2>&1; then
    echo "  launching 'gh auth login' as turing."
    echo "  Choose: GitHub.com → SSH → generate a new key (title: ${HOSTNAME_NEW}-turing) → Login with web browser."
    sudo -u turing -H gh auth login
    sudo -u turing -H gh auth setup-git
else
    echo "  already authenticated, skipping"
fi

log "Phase 3.2 — Clone the repo to /home/turing/turing"
if [[ ! -d /home/turing/turing/.git ]]; then
    # Prefer SSH (gh auth login set up the key); fall back to HTTPS via gh's git credential helper.
    as_turing "git clone $REPO_URL_SSH ~/turing" \
      || as_turing "git clone $REPO_URL_HTTPS ~/turing"
else
    echo "  already cloned, pulling latest"
    as_turing "cd ~/turing && git pull --ff-only" || warn "git pull failed; continuing with existing checkout"
fi

log "Phase 3.3 — Python 3.11 + venv + install Turing"
as_turing "uv python install 3.11"
if [[ ! -x /home/turing/venv/bin/python ]]; then
    as_turing "uv venv --python 3.11 --seed ~/venv"
fi
as_turing "uv pip install --python ~/venv/bin/python --upgrade pip wheel"
as_turing "uv pip install --python ~/venv/bin/python hatchling"
as_turing "uv pip install --python ~/venv/bin/python -e ~/turing"

log "Phase 3.4 — Embedding model"
as_turing "mkdir -p $EMBEDDING_DIR"
if [[ ! -s "$EMBEDDING_DIR/model.onnx" ]]; then
    as_turing "curl -fL -o $EMBEDDING_DIR/model.onnx $EMBEDDING_BASE/onnx/model.onnx"
fi
if [[ ! -s "$EMBEDDING_DIR/tokenizer.json" ]]; then
    as_turing "curl -fL -o $EMBEDDING_DIR/tokenizer.json $EMBEDDING_BASE/tokenizer.json"
fi
ls -lh "$EMBEDDING_DIR"

log "Phase 3.5 — Pull Ollama model ($OLLAMA_MODEL)"
as_turing "ollama pull $OLLAMA_MODEL"

log "Phase 3.6 — Data directory"
as_turing "mkdir -p ~/turing/data"

# ── Phase 4: .env ────────────────────────────────────────────────────
log "Phase 4 — Write .env (worker profile)"
ENV_PATH=/home/turing/turing/.env
if [[ -f "$ENV_PATH" ]]; then
    if confirm "  $ENV_PATH already exists. Overwrite?"; then
        REWRITE_ENV=1
    else
        REWRITE_ENV=0
        echo "  keeping existing .env"
    fi
else
    REWRITE_ENV=1
fi

if [[ "$REWRITE_ENV" == "1" ]]; then
    sudo -u turing tee "$ENV_PATH" >/dev/null <<EOF
# Turing worker .env — generated by setup-jetson.sh
# This is a worker node. Coordinator-only fields (DISCORD/ANTHROPIC/GATEWAY)
# are intentionally left empty.

TURING_NODE_NAME=$NODE_NAME
TURING_ENV=production
TURING_LOG_LEVEL=INFO

# Local LLM (workers never call cloud directly)
TURING_LLM_ROUTING_MODE=local
TURING_OLLAMA_HOST=http://localhost:11434
TURING_OLLAMA_MODEL=$OLLAMA_MODEL

# Memory / data
TURING_DB_PATH=/home/turing/turing/data/turing.db
TURING_EMBEDDING_MODEL_PATH=$EMBEDDING_DIR

# Sandbox
TURING_SANDBOX_ENABLED=true
TURING_SANDBOX_TIMEOUT=30
TURING_ALLOWED_WRITE_PATHS=["/tmp","/home/turing/turing/data"]

# Disabled on workers
TURING_MESH_ENABLED=false
TURING_GATEWAY_ENABLED=false

# Coordinator-only — leave blank on workers
TURING_DISCORD_TOKEN=
TURING_ANTHROPIC_API_KEY=
TURING_GATEWAY_TOKEN=
EOF
    sudo chmod 600 "$ENV_PATH"
    sudo chown turing:turing "$ENV_PATH"
fi

# ── Phase 5: systemd ─────────────────────────────────────────────────
log "Phase 5 — Install and start the systemd service"
sudo tee /etc/systemd/system/turing.service >/dev/null <<'SERVICE'
[Unit]
Description=Turing AI Assistant (worker)
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
sudo systemctl restart turing

sleep 3
sudo systemctl status turing --no-pager || true

# ── Done ─────────────────────────────────────────────────────────────
log "Done."
cat <<EOF

Next steps:
  • Watch the logs:    sudo journalctl -u turing -f
  • Check the GPU:     jtop
  • Reboot to confirm autostart:   sudo reboot

If the service is not 'active (running)', see Appendix A in
docs/hardware/jetson-orin-nano.md.
EOF
