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
# This script implements PHASES 1–7:
#   1 system  2 user  3 repo+app  (slice G1)
#   4 secrets  5 NATS nkey/TLS/LAN-only  6 vault  7 systemd  (slice G2)
# All phases are idempotent — see each phase header for its re-run behaviour.
#
# One interactive hand-off you cannot skip:
#   1. `tailscale up` prints a URL to authenticate this WSL2 node. Tailscale
#      lives INSIDE WSL2 (the Windows host is not a Tailnet node — ADR 0010 §8).
#
# Two operator hand-offs Phase 5 produces (FIRST run only):
#   • 4 worker NATS seeds + the coordinator NATS URL are PRINTED ONCE for you to
#     paste into each Jetson's .env (TURING_NATS_NKEY_SEED / TURING_NATS_URL).
#     They are not re-printed on re-run, but are kept (root, 0600) as a recovery
#     safety net under /etc/turing/nats/workers/ — capture them when they appear,
#     and shred that dir once every Jetson is provisioned.
#
# Everything else is automated.

set -euo pipefail

# ── tunables ─────────────────────────────────────────────────────────
REPO_URL_SSH="git@github.com:oap22/Turing.git"
REPO_URL_HTTPS="https://github.com/oap22/Turing.git"
EMBEDDING_DIR="/home/turing/turing/models/all-MiniLM-L6-v2"
EMBEDDING_BASE="https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main"

# Phase 4 — secrets bootstrap (operator pre-stages the first; we shred it).
ENV_BOOTSTRAP="/home/turing/.env.coordinator.bootstrap"
ENV_COORDINATOR="/home/turing/turing/.env.coordinator"

# Phase 5 — NATS nkey/TLS/LAN-only. The `nk` keygen binary ships in the
# nats-io/nkeys .deb (versioned-only assets, so we resolve the tag at runtime;
# mirrors Phase 1.5's nats-server .deb fetch). 4 worker seeds matches the
# Jetson fleet (ADR 0010 §5/§9).
NATS_ETC_DIR="/etc/turing/nats"             # coordinator key + TLS material (root)
NATS_CONF="/etc/nats-server.conf"           # the .deb's nats-server.service reads this
NATS_PORT="4222"                            # LAN-only listen port (Tailnet-reachable)
WORKER_COUNT="4"
NKEYS_REPO="nats-io/nkeys"

# Phase 6 — vault git repo (ADR 0010 §4). Placeholder default; the operator
# points it at their private vault repo (slice I creates it). Empty/placeholder
# is tolerated — Phase 6 skips the clone and just ensures the watcher path.
VAULT_DIR="/home/turing/vault"
VAULT_REPO_URL="${TURING_VAULT_REPO_URL:-}"  # e.g. git@github.com:oap22/turing-vault.git

# Phase 7 — the three turing-* systemd units (ADR 0010 §6).
COORD_UNITS=(turing-coordinator turing-gateway turing-vault-watcher)

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

log "Turing Surface/WSL2 coordinator setup (phases 1–7)"
echo "This will provision the current WSL2 distro as the Turing coordinator:"
echo "  1 system  2 user  3 repo+app  4 secrets  5 NATS keys  6 vault  7 systemd"
echo "Phase 5 prints the 4 worker NATS seeds + coordinator URL ONCE on first run."

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
# Everything Phase 1 invokes that isn't a base/coreutils/systemd tool must be
# declared here, because `set -euo pipefail` aborts the whole run on the first
# missing binary — and Phase 1 runs before the turing user/repo exist. A fresh
# WSL2 Ubuntu rootfs is minimal: curl (1.2/1.4/1.5/3.x) and gnupg (gpg, 1.6's
# apt-key dearmor) are frequently absent; git (3.1) and ca-certificates (TLS
# for every curl) likewise. nats-server's .deb is fetched via curl + dpkg (base).
sudo apt-get update
sudo apt-get install -y \
    build-essential libsqlite3-dev bubblewrap curl git zstd \
    nano python3-pip ca-certificates gnupg

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
# Re-download only files that are absent or empty; `-s` (non-empty) treats a
# truncated/0-byte file from an interrupted run as missing (matches setup-jetson.sh).
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

# ── Phase 4: secrets bootstrap ───────────────────────────────────────
# The operator pre-stages /home/turing/.env.coordinator.bootstrap (Anthropic
# key, gateway token, ntfy base-url/topic, etc.) before running this. We copy it
# into place as the coordinator .env (0600, turing-owned) and shred the
# bootstrap so the plaintext secret doesn't linger in $HOME.
# Idempotent: once .env.coordinator exists we keep it (operator may have edited
# it) and the bootstrap is already gone, so a re-run is a clean no-op.
log "Phase 4.1 — Coordinator secrets (.env.coordinator)"
if [[ -f "$ENV_COORDINATOR" ]]; then
    echo "  $ENV_COORDINATOR already present, keeping (delete to re-bootstrap)"
    # If a stale bootstrap somehow survived a prior interrupted run, clear it so
    # the plaintext copy never outlives the install.
    if sudo test -f "$ENV_BOOTSTRAP"; then
        warn "Stale $ENV_BOOTSTRAP found alongside an existing .env.coordinator — shredding it."
        sudo shred -u "$ENV_BOOTSTRAP"
    fi
elif sudo test -f "$ENV_BOOTSTRAP"; then
    echo "  reading pre-staged $ENV_BOOTSTRAP"
    # install (not cp) so mode/owner are set atomically before any content lands
    # under turing's view; -T treats the dest as a file, not a dir.
    sudo install -T -m 600 -o turing -g turing "$ENV_BOOTSTRAP" "$ENV_COORDINATOR"
    echo "  wrote $ENV_COORDINATOR (0600, turing:turing)"
    sudo shred -u "$ENV_BOOTSTRAP"
    echo "  shredded $ENV_BOOTSTRAP"
else
    die "No secrets found. Pre-stage $ENV_BOOTSTRAP (see docs/coordinator) then re-run.
   It must define at least TURING_ANTHROPIC_API_KEY and TURING_GATEWAY_TOKEN; for
   the cold-start ntfy ping also TURING_COORDINATOR_NTFY_BASE_URL + TURING_OPERATOR_NTFY_TOPIC."
fi

log "Phase 4.2 — Coordinator role flags → .env.coordinator"
# `python -m turing` (turing-coordinator.service) conditionally starts the mesh
# node, the alerts dispatcher, and the in-process webui gateway off these two
# flags (src/turing/__main__.py steps 6/8/8b). Without them the coordinator
# boots the agent loop alone — no NATS peer presence, no ntfy alert fan-out, no
# webui — and the turing-gateway.service in-process gateway the docs advertise
# never actually serves. They live in the bootstrap for production, but a hand
# -staged bootstrap may omit them, so set the coordinator's defaults here.
# (The alerts dispatcher has no separate flag — it activates whenever mesh is on
#  and presence connects; see __main__.py step 8.)
# Idempotent: rewrite an existing line, else append. The file is turing-owned
# 0600, so edit it as turing. Never downgrade a value the operator set to true.
set_coord_flag() {
    # $1 = key, $2 = value to ensure when the key is absent
    local key="$1" value="$2"
    if as_turing "grep -q '^${key}=' '$ENV_COORDINATOR'" 2>/dev/null; then
        echo "  ${key} already set, keeping operator's value"
    else
        as_turing "printf '%s=%s\n' '$key' '$value' >> '$ENV_COORDINATOR'"
        echo "  appended ${key}=${value}"
    fi
}
set_coord_flag TURING_MESH_ENABLED true
set_coord_flag TURING_GATEWAY_ENABLED true

# ── Phase 5: NATS nkey auth + TLS, LAN-only ──────────────────────────
# First run only: generate 5 user nkey pairs (1 coordinator + 4 worker), store
# the coordinator seed under /etc/turing/nats/ (root, 0600), bake the worker
# PUBLIC keys into nats-server.conf, and PRINT each worker seed + the
# coordinator NATS URL once for manual paste into each Jetson's .env
# (ADR 0010 §5/§9). Re-runs detect the existing coordinator key and skip
# generation entirely — seeds are never regenerated or re-printed.
#
# SEED-DURABILITY ORDERING (recon #3): $COORD_SEED_FILE is BOTH the
# coordinator's seed AND the re-run sentinel (the `sudo test -f` guard below).
# It is therefore written LAST — only after the worker seeds are persisted
# durably under $WORKER_SEED_DIR, the TLS cert is minted, nats-server.conf is
# rendered, and the coordinator seed/URL are in .env.coordinator. If any of
# those steps fails, the sentinel is still absent, so a re-run re-generates and
# re-prints rather than tripping the guard with the worker seeds already lost.
log "Phase 5.1 — NATS key material directory"
sudo mkdir -p -m 755 "$NATS_ETC_DIR"

COORD_SEED_FILE="$NATS_ETC_DIR/coordinator.seed"
COORD_PUB_FILE="$NATS_ETC_DIR/coordinator.nkey"          # public key (U...)
WORKER_SEED_DIR="$NATS_ETC_DIR/workers"                  # durable worker-seed safety net
NATS_TLS_CERT="$NATS_ETC_DIR/nats-server.crt"
NATS_TLS_KEY="$NATS_ETC_DIR/nats-server.key"

# The Tailnet hostname workers dial. Prefer the live Tailscale DNS name; fall
# back to the static hostname so a not-yet-authenticated box still produces a
# sensible URL the operator can correct.
TS_DNSNAME="$(tailscale status --json 2>/dev/null \
    | grep -m1 '"DNSName"' | sed -E 's/.*"DNSName": *"([^"]+)\.?".*/\1/' || true)"
NATS_HOST="${TS_DNSNAME:-$HOSTNAME_NEW}"
NATS_URL="tls://${NATS_HOST}:${NATS_PORT}"

log "Phase 5.2 — nkey generation (first run only)"
if sudo test -f "$COORD_SEED_FILE"; then
    echo "  $COORD_SEED_FILE present — keys already generated, skipping (idempotent)."
    echo "  worker seeds were printed on the first run; re-run does NOT re-print them."
else
    # `nk` is the nats-io keygen tool. Its release assets are versioned-only
    # (no `latest` alias), so resolve the tag, then fetch the .deb (same pattern
    # as Phase 1.5's nats-server install).
    if ! command -v nk >/dev/null 2>&1; then
        echo "  installing nk (nats-io/nkeys)"
        NK_ARCH="$(dpkg --print-architecture)"
        NK_TAG="$(curl -fsSL "https://api.github.com/repos/${NKEYS_REPO}/releases/latest" \
            | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')"
        [[ -n "$NK_TAG" ]] || die "Could not resolve latest nats-io/nkeys release tag."
        NK_TMP="$(mktemp -d)"
        curl -fsSL -o "$NK_TMP/nkeys.deb" \
            "https://github.com/${NKEYS_REPO}/releases/download/${NK_TAG}/nkeys-${NK_TAG}-${NK_ARCH}.deb"
        sudo dpkg -i "$NK_TMP/nkeys.deb"
        rm -rf "$NK_TMP"
    fi
    nk -version 2>/dev/null || nk --version 2>/dev/null || true

    # Generate the coordinator pair into a TEMP seed file (NOT the sentinel
    # path yet — see the durability note in the Phase 5 header). Derive its
    # public key from the seed. umask 077 keeps every seed 0600 from birth.
    echo "  generating coordinator + ${WORKER_COUNT} worker nkey pairs"
    sudo install -d -m 700 "$NATS_ETC_DIR"
    COORD_SEED_TMP="$NATS_ETC_DIR/.coordinator.seed.tmp"
    sudo bash -c "umask 077 && nk -gen user > '$COORD_SEED_TMP'"
    sudo bash -c "nk -inkey '$COORD_SEED_TMP' -pubout > '$COORD_PUB_FILE'"
    sudo chmod 600 "$COORD_SEED_TMP"
    sudo chmod 644 "$COORD_PUB_FILE"
    COORD_PUB="$(sudo cat "$COORD_PUB_FILE")"
    COORD_SEED="$(sudo cat "$COORD_SEED_TMP")"

    # Worker pairs: keep public keys for the server config; collect seeds to
    # print once. Worker seeds are persisted durably under $WORKER_SEED_DIR
    # (root, 0600) BEFORE the sentinel is written, so an interrupted run never
    # strands them — a re-run that finds the sentinel absent regenerates and
    # re-prints. They still never leave the coordinator's protected dir; the
    # operator pastes them onto each Jetson and may shred the dir afterwards.
    sudo install -d -m 700 "$WORKER_SEED_DIR"
    WORKER_PUBS=()
    WORKER_SEEDS=()
    for n in $(seq 1 "$WORKER_COUNT"); do
        wseed_file="$WORKER_SEED_DIR/worker-${n}.seed"
        sudo bash -c "umask 077 && nk -gen user > '$wseed_file'"
        sudo chmod 600 "$wseed_file"
        wseed="$(sudo cat "$wseed_file")"
        wpub="$(printf '%s\n' "$wseed" | sudo nk -inkey /dev/stdin -pubout)"
        WORKER_PUBS+=("$wpub")
        WORKER_SEEDS+=("$wseed")
    done

    # Self-signed TLS for the LAN listener so transport is encrypted on day one
    # (ADR 0010 §5; ADR 0001 "TLS + nkey from day one"). The operator may swap
    # in a Tailscale-issued cert later — the config path stays the same.
    if ! sudo test -f "$NATS_TLS_CERT"; then
        echo "  minting self-signed TLS cert for nats-server (CN=$NATS_HOST)"
        # subjectAltName is required: modern TLS clients (the workers' NATS
        # client) verify the hostname against the SAN, not the CN, so a cert
        # without it fails the handshake (ADR 0010 §5 "TLS + nkey from day one").
        # Cover the Tailnet DNS name workers dial plus loopback for local checks.
        sudo openssl req -x509 -newkey rsa:2048 -nodes \
            -keyout "$NATS_TLS_KEY" -out "$NATS_TLS_CERT" \
            -days 3650 -subj "/CN=${NATS_HOST}" \
            -addext "subjectAltName=DNS:${NATS_HOST},DNS:localhost,IP:127.0.0.1" \
            >/dev/null 2>&1 \
            || die "openssl cert generation failed (is openssl installed?)."
        sudo chmod 600 "$NATS_TLS_KEY"
        sudo chmod 644 "$NATS_TLS_CERT"
    fi

    # Render nats-server.conf: LAN-only listen, TLS, nkey-authorized users
    # (coordinator + 4 worker PUBLIC keys). The .deb's nats-server.service reads
    # this path. Built in a temp file then installed atomically.
    log "Phase 5.3 — nats-server.conf (TLS + nkey, LAN-only)"
    NATS_CONF_TMP="$(mktemp)"
    {
        echo "# nats-server.conf — generated by setup-coordinator.sh (ADR 0010 §5)."
        echo "# LAN-only (listen on the Tailnet/loopback interface), TLS + nkey auth."
        echo "# Worker access is by PUBLIC nkey; the matching seeds are pasted onto each"
        echo "# Jetson and kept (root, 0600) as a recovery safety net under"
        echo "# $WORKER_SEED_DIR (shred it once every worker is provisioned)."
        echo "# Regenerate by deleting $COORD_SEED_FILE and re-running"
        echo "# setup-coordinator.sh (this re-issues ALL seeds)."
        echo "listen: \"0.0.0.0:${NATS_PORT}\""
        echo ""
        echo "tls {"
        echo "  cert_file: \"${NATS_TLS_CERT}\""
        echo "  key_file:  \"${NATS_TLS_KEY}\""
        echo "}"
        echo ""
        echo "authorization {"
        echo "  users = ["
        echo "    { nkey: ${COORD_PUB} }   # coordinator"
        for idx in "${!WORKER_PUBS[@]}"; do
            echo "    { nkey: ${WORKER_PUBS[$idx]} }   # worker $((idx + 1))"
        done
        echo "  ]"
        echo "}"
    } >"$NATS_CONF_TMP"
    sudo install -m 600 "$NATS_CONF_TMP" "$NATS_CONF"
    rm -f "$NATS_CONF_TMP"

    # The nats-server .deb ships nats-server.service reading $NATS_CONF; enable
    # + (re)start it now that the config exists.
    sudo systemctl enable nats-server >/dev/null 2>&1 || true
    sudo systemctl restart nats-server || warn "nats-server failed to start — check: sudo journalctl -u nats-server -n50"

    # Phase 5.3b — coordinator's OWN seed + URL → .env.coordinator. Without
    # these, `python -m turing` reads config.nats_nkey_seed=None / the localhost
    # default URL and cannot authenticate to its own bus when TURING_MESH_ENABLED
    # is on (Phase 4.2) — mesh presence + the alerts dispatcher silently degrade
    # to singleton. Same idempotent rewrite-or-append the vault path uses; the
    # file is turing-owned 0600, so edit it as turing.
    log "Phase 5.3b — Coordinator NATS seed + URL → .env.coordinator"
    if as_turing "grep -q '^TURING_NATS_NKEY_SEED=' '$ENV_COORDINATOR'" 2>/dev/null; then
        as_turing "sed -i 's#^TURING_NATS_NKEY_SEED=.*#TURING_NATS_NKEY_SEED=$COORD_SEED#' '$ENV_COORDINATOR'"
        echo "  updated TURING_NATS_NKEY_SEED"
    else
        as_turing "printf 'TURING_NATS_NKEY_SEED=%s\n' '$COORD_SEED' >> '$ENV_COORDINATOR'"
        echo "  appended TURING_NATS_NKEY_SEED"
    fi
    if as_turing "grep -q '^TURING_NATS_URL=' '$ENV_COORDINATOR'" 2>/dev/null; then
        as_turing "sed -i 's#^TURING_NATS_URL=.*#TURING_NATS_URL=$NATS_URL#' '$ENV_COORDINATOR'"
        echo "  updated TURING_NATS_URL=$NATS_URL"
    else
        as_turing "printf 'TURING_NATS_URL=%s\n' '$NATS_URL' >> '$ENV_COORDINATOR'"
        echo "  appended TURING_NATS_URL=$NATS_URL"
    fi

    # Print the operator hand-off. The worker seeds are already durable under
    # $WORKER_SEED_DIR (so this print is recoverable if the terminal scrolls),
    # but the print is still the primary capture surface — paste them now.
    log "Phase 5.4 — WORKER NATS SEEDS (paste into each Jetson .env)"
    cat <<EOF

  Coordinator NATS URL (paste as TURING_NATS_URL on every worker):
    ${NATS_URL}

EOF
    for idx in "${!WORKER_SEEDS[@]}"; do
        printf '  worker %d  TURING_NATS_NKEY_SEED=%s\n' "$((idx + 1))" "${WORKER_SEEDS[$idx]}"
    done
    cat <<EOF

  On each Jetson set BOTH fields in /home/turing/turing/.env (setup-jetson.sh
  Phase 4, slice H), then restart the worker:  sudo systemctl restart turing
  Safety net: these seeds are also saved (root, 0600) under
    ${WORKER_SEED_DIR}/worker-N.seed
  Re-run does NOT re-print them; recover from that dir if you missed a paste,
  then shred it once every Jetson is provisioned:  sudo rm -rf ${WORKER_SEED_DIR}
EOF

    # Phase 5.5 — commit the sentinel LAST. Every durable artifact above (worker
    # seeds, TLS cert, nats-server.conf, coordinator seed/URL in .env) now
    # exists, so promoting the temp coordinator seed to its final path is the
    # single atomic step that arms the re-run guard. A failure before here
    # leaves the sentinel absent → a re-run regenerates cleanly (recon #3).
    log "Phase 5.5 — Commit coordinator seed sentinel (arms the re-run guard)"
    sudo mv "$COORD_SEED_TMP" "$COORD_SEED_FILE"
    sudo chmod 600 "$COORD_SEED_FILE"
    echo "  $COORD_SEED_FILE written — re-runs now skip key generation"
fi

# ── Phase 6: vault git repo + watcher path ───────────────────────────
# The vault lives on WSL2 ext4 under the turing user (ADR 0010 §4) — NOT under
# /mnt/c (inotify is unreliable there). Clone the operator's vault repo if set;
# otherwise just ensure the directory exists so the watcher has a path. The
# "diff last commit, reindex" watcher impl is slice I.
# Idempotent: an existing checkout is pulled (best-effort); a bare dir is left.
log "Phase 6.1 — Vault git repo at $VAULT_DIR"
if [[ -d "$VAULT_DIR/.git" ]]; then
    echo "  already cloned, pulling latest"
    as_turing "cd '$VAULT_DIR' && git pull --ff-only" \
        || warn "vault git pull failed; continuing with existing checkout"
elif [[ -n "$VAULT_REPO_URL" ]]; then
    echo "  cloning $VAULT_REPO_URL → $VAULT_DIR"
    as_turing "git clone '$VAULT_REPO_URL' '$VAULT_DIR'" \
        || die "vault clone failed. Check TURING_VAULT_REPO_URL and the turing user's git auth."
else
    warn "TURING_VAULT_REPO_URL unset — creating an empty $VAULT_DIR (slice I creates the repo)."
    as_turing "mkdir -p '$VAULT_DIR'"
fi

log "Phase 6.2 — Watcher path → .env.coordinator (TURING_VAULT_PATH)"
# Point the (slice-I) watcher at the vault. Idempotent: rewrite the line if
# present, else append. The file is turing-owned 0600, so edit it as turing.
if as_turing "grep -q '^TURING_VAULT_PATH=' '$ENV_COORDINATOR'" 2>/dev/null; then
    as_turing "sed -i 's#^TURING_VAULT_PATH=.*#TURING_VAULT_PATH=$VAULT_DIR#' '$ENV_COORDINATOR'"
    echo "  updated TURING_VAULT_PATH=$VAULT_DIR"
else
    as_turing "printf '\nTURING_VAULT_PATH=%s\n' '$VAULT_DIR' >> '$ENV_COORDINATOR'"
    echo "  appended TURING_VAULT_PATH=$VAULT_DIR"
fi

# ── Phase 7: the three turing-* systemd units ────────────────────────
# Install/start turing-coordinator, turing-gateway, turing-vault-watcher
# (ADR 0010 §6) — each Restart=always. ONLY turing-coordinator fires the
# cold-start ntfy ping (its ExecStartPost). Idempotent: an unchanged unit file
# is left in place (no needless restart); a changed one triggers daemon-reload
# + restart, matching the ntfy.service handling above and setup-jetson.sh.
log "Phase 7 — Install and start the three turing-* systemd units"
for unit in "${COORD_UNITS[@]}"; do
    UNIT_SRC="$SCRIPT_DIR/coordinator/${unit}.service"
    UNIT_DST="/etc/systemd/system/${unit}.service"
    [[ -f "$UNIT_SRC" ]] || die "Missing unit template $UNIT_SRC."
    if [[ -f "$UNIT_DST" ]] && sudo cmp -s "$UNIT_SRC" "$UNIT_DST"; then
        echo "  ${unit}.service unchanged, skipping rewrite"
        unit_changed=0
    else
        sudo install -m 644 "$UNIT_SRC" "$UNIT_DST"
        sudo systemctl daemon-reload
        unit_changed=1
    fi
    sudo systemctl enable "$unit" >/dev/null 2>&1 || true
    if [[ "$unit_changed" == "1" ]] || ! sudo systemctl is-active --quiet "$unit"; then
        sudo systemctl restart "$unit"
    else
        echo "  ${unit} already active, leaving it running"
    fi
done
sleep 2
for unit in "${COORD_UNITS[@]}"; do
    sudo systemctl status "$unit" --no-pager --lines=0 || true
done

# ── Done (phases 1–7) ────────────────────────────────────────────────
log "Done — phases 1–7 complete."
cat <<EOF

The coordinator host is fully provisioned: hostname set, Tailscale up inside
WSL2, nats-server (TLS + nkey, LAN-only) + ntfy running, the turing service
user, the Turing editable install with the embedding model, the coordinator
secrets, the vault checkout, and the three turing-* systemd units.

Active units:
  • turing-coordinator  (agent loop, scheduler, planner, budget gate, alerts,
                          in-process webui gateway) — fires the cold-start ntfy ping
  • turing-gateway       (reserved for the gateway/coordinator process split)
  • turing-vault-watcher (reserved for the slice-I vault watcher)
  • nats-server, ntfy, tailscaled (stock)

If Phase 5 ran for the FIRST time, the 4 worker NATS seeds + coordinator URL
were printed above — paste them into each Jetson's .env now (they are not
stored here and won't be re-printed).

Next steps:
  • Watch the coordinator:  sudo journalctl -u turing-coordinator -f
  • Check NATS:             sudo systemctl status nats-server --no-pager
  • Check ntfy:             sudo systemctl status ntfy --no-pager
  • Check Tailscale:        tailscale status
  • Sanity-import:          sudo -u turing -H /home/turing/venv/bin/python -c 'import turing; print(turing.__version__)'
EOF
