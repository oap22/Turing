#!/usr/bin/env bash
# Surface/WSL2 coordinator-node setup for Turing (ADR 0010 §5).
#
# Run INSIDE the WSL2 Ubuntu distro on the Surface — the coordinator host
# (the one node with Discord-retired alerts/ntfy, the gateway, and the
# vault). Stand up the underlying WSL2 distro first with the Windows-side
# scripts/bootstrap-surface.ps1. Idempotent: re-running skips steps that
# already completed.
#
#   bash scripts/setup-coordinator.sh
#
# This script implements PHASES 1–3 only (system, service user, repo + app).
# Phases 4–7 (secrets, NATS nkey generation, vault, the three systemd units)
# land in a follow-on slice — see docs/adr/0010-slices.md, slice G.
#
# One interactive hand-off you cannot skip:
#   1. `tailscale up` prints a URL to authenticate this WSL2 node. Tailscale
#      lives INSIDE WSL2 (the Windows host is not a Tailnet node — ADR 0010 §8).
#
# Everything else is automated.

set -euo pipefail

# ── tunables ─────────────────────────────────────────────────────────
REPO_URL_SSH="git@github.com:oap22/Turing.git"
REPO_URL_HTTPS="https://github.com/oap22/Turing.git"
EMBEDDING_DIR="/home/turing/turing/models/all-MiniLM-L6-v2"
EMBEDDING_BASE="https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main"

# ── helpers ──────────────────────────────────────────────────────────
log()  { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

require_not_root() {
    [[ $EUID -ne 0 ]] || die "Run as your sudo-capable WSL2 user, not root. The script will sudo where needed."
}

require_wsl2() {
    # The coordinator lives inside WSL2 Ubuntu on the Surface. Refuse to run on
    # a bare Linux box, a Jetson, or the Windows host — the Tailscale-in-WSL2
    # and systemd assumptions below only hold under WSL2 (ADR 0010 §5/§8).
    [[ -r /proc/sys/kernel/osrelease ]] \
        || die "Not WSL2: /proc/sys/kernel/osrelease missing."
    local osrelease
    osrelease="$(tr '[:upper:]' '[:lower:]' </proc/sys/kernel/osrelease)"
    [[ "$osrelease" == *microsoft* || "$osrelease" == *wsl* ]] \
        || die "Not running inside WSL2 (osrelease: $(cat /proc/sys/kernel/osrelease)). This script is WSL2-Ubuntu only."
    echo "  detected WSL2 (osrelease: $(cat /proc/sys/kernel/osrelease))"
}

# Run a command as the `turing` user in a login shell so PATH/HOME are right.
as_turing() { sudo -u turing -H bash -lc "$*"; }

# ── prompts ──────────────────────────────────────────────────────────
require_not_root

log "Verifying this is a WSL2 host"
require_wsl2

log "Turing Surface/WSL2 coordinator setup (phases 1–3)"
echo "This will provision the current WSL2 distro as the Turing coordinator."
echo "Phases 4–7 (secrets, NATS keys, vault, systemd units) are a later slice."

# Re-runs must not re-prompt for a value that's already been set. Seed the
# default from the current static hostname so the operator can press Enter
# (or run unattended / piped from /dev/null) and keep what's already there.
# `|| true` keeps `set -e` happy when stdin is closed (EOF on re-run).
HOSTNAME_CURRENT="$(hostnamectl --static)"
read -r -p "Hostname for this coordinator (e.g. surface) [${HOSTNAME_CURRENT}]: " HOSTNAME_NEW || true
HOSTNAME_NEW="${HOSTNAME_NEW:-$HOSTNAME_CURRENT}"
[[ -n "$HOSTNAME_NEW" ]] || die "Hostname is required (no current hostname to fall back to)."

# ── Phase 1: system provisioning ─────────────────────────────────────
log "Phase 1.1 — Set hostname to $HOSTNAME_NEW"
if [[ "$(hostnamectl --static)" != "$HOSTNAME_NEW" ]]; then
    sudo hostnamectl set-hostname "$HOSTNAME_NEW"
else
    echo "  already set, skipping"
fi

log "Phase 1.2 — Tailscale (inside WSL2 only — the Windows host is not a Tailnet node)"
if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
else
    echo "  already installed, skipping"
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

log "Phase 1.4 — uv (for Python 3.11)"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh \
      | sudo env UV_INSTALL_DIR=/usr/local/bin sh
else
    echo "  already installed, skipping"
fi
uv --version

log "Phase 1.5 — nats-server (coordinator message bus)"
# Install the binary only. Per ADR 0010 §5, TLS + nkey auth and the worker
# public-key config land in Phase 5 (a later slice); here we just get the
# server on PATH so that phase has something to configure.
if ! command -v nats-server >/dev/null 2>&1; then
    NATS_ARCH="$(dpkg --print-architecture)"   # amd64 on the Surface
    NATS_TMP="$(mktemp -d)"
    curl -fsSL -o "$NATS_TMP/nats-server.deb" \
      "https://github.com/nats-io/nats-server/releases/latest/download/nats-server-latest-${NATS_ARCH}.deb"
    sudo dpkg -i "$NATS_TMP/nats-server.deb"
    rm -rf "$NATS_TMP"
else
    echo "  already installed, skipping"
fi
nats-server --version

log "Phase 1.6 — ntfy server (closed-laptop alert push — ADR 0010 §2)"
# Install the ntfy binary via its apt repo, then drop the config + systemd unit
# checked in under scripts/coordinator/. The dispatcher cut-over to ntfy is
# slice B; here we only make the server installable and running so A's verify
# step (curl a topic from a Jetson) works. Idempotent with Wave-1 slice A.
if ! command -v ntfy >/dev/null 2>&1; then
    sudo mkdir -p -m 755 /etc/apt/keyrings
    curl -fsSL https://archive.heckel.io/apt/pubkey.txt \
      | sudo gpg --dearmor -o /etc/apt/keyrings/ntfy.gpg
    sudo chmod 644 /etc/apt/keyrings/ntfy.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/ntfy.gpg] https://archive.heckel.io/apt debian main" \
      | sudo tee /etc/apt/sources.list.d/ntfy.list >/dev/null
    sudo apt-get update
    sudo apt-get install -y ntfy
else
    echo "  already installed, skipping"
fi

# ntfy runs as a dedicated unprivileged service user (matches ntfy.service).
if ! id ntfy >/dev/null 2>&1; then
    sudo adduser --system --no-create-home --group ntfy
else
    echo "  ntfy user already exists, skipping"
fi
sudo mkdir -p /etc/ntfy /var/cache/ntfy
sudo chown ntfy:ntfy /var/cache/ntfy

# server.yml — install from the repo template if absent/changed. The operator
# edits base-url for their tailnet (the checked-in value is a placeholder), so
# never clobber an already-customised file.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
NTFY_YML_SRC="$SCRIPT_DIR/coordinator/server.yml"
NTFY_YML_DST="/etc/ntfy/server.yml"
if [[ ! -f "$NTFY_YML_DST" ]]; then
    sudo install -m 644 "$NTFY_YML_SRC" "$NTFY_YML_DST"
    warn "Installed $NTFY_YML_DST — edit base-url to your tailnet name before relying on alerts."
else
    echo "  $NTFY_YML_DST already present, keeping (delete to reinstall from template)"
fi

# ntfy.service — install/refresh from the repo template, daemon-reload on change.
NTFY_UNIT_SRC="$SCRIPT_DIR/coordinator/ntfy.service"
NTFY_UNIT_DST="/etc/systemd/system/ntfy.service"
if [[ -f "$NTFY_UNIT_DST" ]] && sudo cmp -s "$NTFY_UNIT_SRC" "$NTFY_UNIT_DST"; then
    echo "  ntfy.service unchanged, skipping rewrite"
    NTFY_UNIT_CHANGED=0
else
    sudo install -m 644 "$NTFY_UNIT_SRC" "$NTFY_UNIT_DST"
    sudo systemctl daemon-reload
    NTFY_UNIT_CHANGED=1
fi
sudo systemctl enable ntfy >/dev/null 2>&1 || true
if [[ "$NTFY_UNIT_CHANGED" == "1" ]] || ! sudo systemctl is-active --quiet ntfy; then
    sudo systemctl restart ntfy
else
    echo "  ntfy already active, leaving it running"
fi

# ── Phase 2: turing user ─────────────────────────────────────────────
log "Phase 2.1 — Create the unprivileged turing service user"
if ! id turing >/dev/null 2>&1; then
    sudo adduser --disabled-password --gecos "" turing
    echo "  user created (no password set; reach it via sudo). Set one with: sudo passwd turing"
else
    echo "  already exists, skipping"
fi

# ── Phase 3: code deploy as turing ───────────────────────────────────
log "Phase 3.1 — Clone the repo to /home/turing/turing"
if [[ ! -d /home/turing/turing/.git ]]; then
    # Prefer SSH; fall back to HTTPS for a fresh box with no key configured yet.
    # (GitHub auth for the turing user is handled in a later slice alongside the
    # secrets bootstrap; an unauthenticated HTTPS clone of the public repo works.)
    as_turing "git clone $REPO_URL_SSH ~/turing" \
      || as_turing "git clone $REPO_URL_HTTPS ~/turing"
else
    echo "  already cloned, pulling latest"
    as_turing "cd ~/turing && git pull --ff-only" || warn "git pull failed; continuing with existing checkout"
fi

log "Phase 3.2 — Python 3.11 + venv + install Turing (editable)"
as_turing "uv python install 3.11"
if [[ ! -x /home/turing/venv/bin/python ]]; then
    as_turing "uv venv --python 3.11 --seed ~/venv"
else
    echo "  venv already present, skipping create"
fi
as_turing "uv pip install --python ~/venv/bin/python --upgrade pip wheel"
as_turing "uv pip install --python ~/venv/bin/python hatchling"
as_turing "uv pip install --python ~/venv/bin/python -e ~/turing"

log "Phase 3.3 — Embedding model"
as_turing "mkdir -p $EMBEDDING_DIR"
if [[ ! -s "$EMBEDDING_DIR/model.onnx" ]]; then
    as_turing "curl -fL -o $EMBEDDING_DIR/model.onnx $EMBEDDING_BASE/onnx/model.onnx"
else
    echo "  model.onnx already present, skipping download"
fi
if [[ ! -s "$EMBEDDING_DIR/tokenizer.json" ]]; then
    as_turing "curl -fL -o $EMBEDDING_DIR/tokenizer.json $EMBEDDING_BASE/tokenizer.json"
else
    echo "  tokenizer.json already present, skipping download"
fi
ls -lh "$EMBEDDING_DIR"

# ── Done (phases 1–3) ────────────────────────────────────────────────
log "Done — phases 1–3 complete."
cat <<EOF

The coordinator host now has: hostname set, Tailscale up inside WSL2,
nats-server + ntfy installed (ntfy running), the turing service user, and a
Turing editable install with the embedding model under
/home/turing/turing.

NOT yet configured (phases 4–7, a later slice):
  • secrets bootstrap (.env.coordinator)
  • NATS TLS + nkey auth and the 5 worker seeds
  • vault git clone + watcher
  • the three turing-* systemd units (coordinator / gateway / vault-watcher)

Next steps for now:
  • Check ntfy:        sudo systemctl status ntfy --no-pager
  • Check Tailscale:   tailscale status
  • Sanity-import:     sudo -u turing -H /home/turing/venv/bin/python -c 'import turing; print(turing.__version__)'
EOF
