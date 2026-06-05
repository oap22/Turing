#!/usr/bin/env bash
# Fully automated Jetson-fleet provisioning, driven from the coordinator.
#
# Runs ONE command on the Surface coordinator and provisions every Jetson
# worker over SSH (Tailnet or LAN) — no per-node keyboard time. It closes the
# two manual hand-offs that setup-jetson.sh leaves to a human:
#
#   1. NATS seed distribution. setup-coordinator.sh (Phase 5) generated one
#      nkey seed per worker and saved them under /etc/turing/nats/workers/
#      (root, 0600). This script reads worker-N.seed and feeds it to node N's
#      setup-jetson.sh as TURING_NATS_NKEY_SEED — no copy-paste.
#   2. Per-node GitHub auth. The repo is private, so instead of a gh device
#      flow on every Jetson we rsync the coordinator's already-checked-out tree
#      to each node (--push-repo, the default) and run setup-jetson.sh with
#      TURING_DEPLOY_SRC + TURING_SKIP_GH_AUTH. Tailscale is necessarily already
#      up on a node we can SSH into, so neither interactive hand-off remains.
#
# Result: `scripts/setup-fleet.sh jetson-1 jetson-2 jetson-3 jetson-4` brings
# up (or idempotently re-provisions / refreshes) the whole fleet unattended.
#
# Usage:
#   scripts/setup-fleet.sh [options] [host ...]
#
# Hosts are the Tailnet hostnames (or LAN IPs) of the Jetsons, IN WORKER ORDER:
# the first host gets worker-1.seed, the second worker-2.seed, and so on. Give
# them positionally, via --hosts, or via $TURING_FLEET_HOSTS (space-separated).
#
# Options:
#   --hosts "h1 h2 ..."  Space-separated host list (alt. to positional args).
#   --user USER          SSH user on each Jetson (sudo-capable; the JetPack
#                        first-boot account). Default: $TURING_FLEET_USER or
#                        the example name from the hardware doc.
#   --nats-url URL       Coordinator NATS URL to hand to every worker. Default:
#                        $TURING_NATS_URL, else read from .env.coordinator, else
#                        derived from `tailscale status`.
#   --seed-dir DIR       Where the durable worker seeds live. Default
#                        /etc/turing/nats/workers (read with sudo).
#   --push-repo          Deploy code by rsync from this tree (default; works
#                        for the private repo with no per-node gh auth).
#   --via-clone          Instead let each node clone from GitHub (needs the
#                        node to already be gh-authed; falls back to the
#                        interactive flow setup-jetson.sh runs).
#   --parallel           Provision all nodes concurrently (per-node logs under
#                        a temp dir). Default is sequential so a first-time gh
#                        device flow (--via-clone) can be completed node by node.
#   --update             Light refresh: rsync code + restart turing.service
#                        only; skip package/model/auth phases. Implies --push-repo.
#   --dry-run            Print the plan and the exact remote commands; do nothing.
#   -h, --help           This help.
#
# Examples:
#   # First-time full bring-up of the 4-node fleet (hands-off):
#   scripts/setup-fleet.sh jetson-1 jetson-2 jetson-3 jetson-4
#
#   # Refresh code on every node and restart, nothing else:
#   scripts/setup-fleet.sh --update --parallel jetson-1 jetson-2 jetson-3 jetson-4
#
#   # Dry-run to see what would happen:
#   scripts/setup-fleet.sh --dry-run jetson-1 jetson-2

set -euo pipefail

# ── repo root (this script lives in scripts/) ────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── defaults / tunables ──────────────────────────────────────────────
SSH_USER="${TURING_FLEET_USER:-allen}"        # JetPack first-boot sudo user (docs' example)
SEED_DIR="${TURING_FLEET_SEED_DIR:-/etc/turing/nats/workers}"
ENV_COORDINATOR="${TURING_ENV_COORDINATOR:-/home/turing/turing/.env.coordinator}"
NATS_PORT_DEFAULT="4222"
DEPLOY_STAGE="turing-deploy"                   # rsync staging dir in the ssh user's home
NATS_URL_OVERRIDE="${TURING_NATS_URL:-}"
DEPLOY_MODE="push"                             # push | clone
PARALLEL=0
UPDATE_ONLY=0
DRY_RUN=0
SSH_OPTS=(-o ConnectTimeout=8 -o BatchMode=no -o StrictHostKeyChecking=accept-new)

HOSTS=()

# ── helpers ──────────────────────────────────────────────────────────
log()  { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; s/^#$//' | sed '$d'; }

# ── arg parse ────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --hosts)     shift; read -r -a _h <<<"${1:-}"; HOSTS+=("${_h[@]}");;
        --user)      shift; SSH_USER="${1:?--user needs a value}";;
        --nats-url)  shift; NATS_URL_OVERRIDE="${1:?--nats-url needs a value}";;
        --seed-dir)  shift; SEED_DIR="${1:?--seed-dir needs a value}";;
        --push-repo) DEPLOY_MODE="push";;
        --via-clone) DEPLOY_MODE="clone";;
        --parallel)  PARALLEL=1;;
        --update)    UPDATE_ONLY=1; DEPLOY_MODE="push";;
        --dry-run)   DRY_RUN=1;;
        -h|--help)   usage; exit 0;;
        --)          shift; while [[ $# -gt 0 ]]; do HOSTS+=("$1"); shift; done; break;;
        -*)          die "unknown option: $1 (try --help)";;
        *)           HOSTS+=("$1");;
    esac
    shift || true
done

# Hosts may also come from the environment.
if [[ ${#HOSTS[@]} -eq 0 && -n "${TURING_FLEET_HOSTS:-}" ]]; then
    read -r -a HOSTS <<<"$TURING_FLEET_HOSTS"
fi
[[ ${#HOSTS[@]} -gt 0 ]] || die "no hosts given. Pass them positionally, via --hosts, or \$TURING_FLEET_HOSTS. See --help."

# ── resolve the coordinator NATS URL once ────────────────────────────
resolve_nats_url() {
    if [[ -n "$NATS_URL_OVERRIDE" ]]; then
        printf '%s' "$NATS_URL_OVERRIDE"; return 0
    fi
    # .env.coordinator is turing-owned 0600 — read it with sudo if present.
    local line=""
    if sudo test -f "$ENV_COORDINATOR" 2>/dev/null; then
        line="$(sudo grep -E '^TURING_NATS_URL=' "$ENV_COORDINATOR" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)"
    fi
    if [[ -n "$line" ]]; then printf '%s' "$line"; return 0; fi
    # Last resort: derive from this coordinator's own Tailnet DNS name.
    local dns
    dns="$(tailscale status --json 2>/dev/null \
        | grep -m1 '"DNSName"' | sed -E 's/.*"DNSName": *"([^"]+)\.?".*/\1/' || true)"
    [[ -n "$dns" ]] || die "could not resolve the coordinator NATS URL (pass --nats-url, or run on the coordinator after setup-coordinator.sh)."
    printf 'tls://%s:%s' "$dns" "$NATS_PORT_DEFAULT"
}

# ── read worker seed for node index (1-based) ────────────────────────
seed_for_index() {
    local idx="$1"
    # Per-host override: $TURING_FLEET_SEED_1 etc. (lets you provision a single
    # node without the durable seed dir present).
    local override_var="TURING_FLEET_SEED_${idx}"
    if [[ -n "${!override_var:-}" ]]; then printf '%s' "${!override_var}"; return 0; fi
    local f="$SEED_DIR/worker-${idx}.seed"
    if sudo test -f "$f" 2>/dev/null; then
        sudo cat "$f"; return 0
    fi
    return 1
}

# ── build the staging tree once (push mode) ──────────────────────────
# rsync excludes mirror what a worker must not receive: VCS/worktree bulk,
# build/venv artifacts, and anything that would bloat the transfer. The
# worker-side rsync (setup-jetson.sh Phase 3.2) additionally protects runtime
# state on the destination; here we just keep the wire payload lean.
RSYNC_EXCLUDES=(
    --exclude='.git/' --exclude='.claude/' --exclude='.venv/'
    --exclude='**/__pycache__/' --exclude='**/node_modules/'
    --exclude='data/' --exclude='models/' --exclude='.env' --exclude='.env.*'
    --exclude='*.pyc'
)

# ── per-node provisioning ────────────────────────────────────────────
provision_node() {
    local idx="$1" host="$2" nats_url="$3"
    local target="${SSH_USER}@${host}"
    local seed; seed="$(seed_for_index "$idx")" || {
        warn "[$host] no seed: $SEED_DIR/worker-${idx}.seed missing and \$TURING_FLEET_SEED_${idx} unset."
        warn "[$host] run setup-coordinator.sh first, or pass the seed via that env var."
        return 1
    }

    # Reachability preflight — fail fast with a clear message.
    if [[ $DRY_RUN -eq 0 ]]; then
        ssh "${SSH_OPTS[@]}" "$target" true 2>/dev/null \
            || { warn "[$host] cannot SSH as $SSH_USER (is it on the Tailnet? is the user right?)"; return 1; }
    fi

    # Remote env handed to setup-jetson.sh. The seed is secret; it rides the
    # encrypted SSH channel and lands only in the worker's 0600 .env. We pass it
    # through the heredoc body (the remote shell's stdin), never on argv, so it
    # is not visible in the node's process table.
    local remote_script
    if [[ $UPDATE_ONLY -eq 1 ]]; then
        remote_script=$(cat <<REMOTE
set -euo pipefail
sudo rsync -a --delete \
    --exclude='.env' --exclude='.env.*' --exclude='data/' --exclude='models/' \
    --exclude='.venv/' --exclude='__pycache__/' --exclude='.git/' --exclude='.claude/' \
    "\$HOME/${DEPLOY_STAGE}/" /home/turing/turing/
sudo chown -R turing:turing /home/turing/turing
sudo systemctl restart turing
sleep 2
sudo systemctl is-active turing
REMOTE
)
    elif [[ "$DEPLOY_MODE" == "push" ]]; then
        remote_script=$(cat <<REMOTE
set -euo pipefail
export TURING_HOSTNAME='${host}'
export TURING_NATS_URL='${nats_url}'
export TURING_NATS_NKEY_SEED='${seed}'
export TURING_DEPLOY_SRC="\$HOME/${DEPLOY_STAGE}"
export TURING_SKIP_GH_AUTH=1
bash "\$HOME/${DEPLOY_STAGE}/scripts/setup-jetson.sh" </dev/null
REMOTE
)
    else  # clone mode
        remote_script=$(cat <<REMOTE
set -euo pipefail
export TURING_HOSTNAME='${host}'
export TURING_NATS_URL='${nats_url}'
export TURING_NATS_NKEY_SEED='${seed}'
if [ ! -d /tmp/turing-bootstrap/.git ]; then
  git clone git@github.com:oap22/Turing.git /tmp/turing-bootstrap \
    || git clone https://github.com/oap22/Turing.git /tmp/turing-bootstrap
else
  git -C /tmp/turing-bootstrap pull --ff-only || true
fi
bash /tmp/turing-bootstrap/scripts/setup-jetson.sh </dev/null
REMOTE
)
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        log "[DRY-RUN] node $idx → $host"
        [[ "$DEPLOY_MODE" == "push" ]] && info "rsync ${REPO_ROOT}/ → ${target}:~/${DEPLOY_STAGE}/"
        info "ssh $target  (worker-${idx} seed: ${seed:0:6}…, nats: $nats_url)"
        printf '%s\n' "$remote_script" | sed 's/^/    | /'
        return 0
    fi

    # Push the staging tree (push + update modes). rsync creates the remote
    # ~/turing-deploy dir itself (remote home exists), so no separate mkdir.
    if [[ "$DEPLOY_MODE" == "push" || $UPDATE_ONLY -eq 1 ]]; then
        rsync -az --delete "${RSYNC_EXCLUDES[@]}" \
            -e "ssh ${SSH_OPTS[*]}" \
            "${REPO_ROOT}/" "${target}:${DEPLOY_STAGE}/" \
            || { warn "[$host] rsync of code failed"; return 1; }
    fi

    # Run the remote provisioning. -t gives setup-jetson a TTY for the rare
    # interactive bit in clone mode; push mode is fully non-interactive.
    ssh "${SSH_OPTS[@]}" -t "$target" "bash -s" <<<"$remote_script"
}

# ── main ─────────────────────────────────────────────────────────────
NATS_URL="$(resolve_nats_url)"

log "Turing fleet provisioning"
info "hosts:    ${HOSTS[*]}"
info "ssh user: $SSH_USER"
info "nats url: $NATS_URL"
info "mode:     $([[ $UPDATE_ONLY -eq 1 ]] && echo update || echo "$DEPLOY_MODE")$([[ $PARALLEL -eq 1 ]] && echo " (parallel)")"
info "seeds:    $SEED_DIR/worker-N.seed"
[[ $DRY_RUN -eq 1 ]] && warn "DRY-RUN — no changes will be made"

declare -a RESULTS
RC=0

if [[ $PARALLEL -eq 1 && $DRY_RUN -eq 0 ]]; then
    TMP_LOGS="$(mktemp -d)"
    declare -a PIDS HOSTS_BY_PID
    for i in "${!HOSTS[@]}"; do
        idx=$((i + 1)); host="${HOSTS[$i]}"
        ( provision_node "$idx" "$host" "$NATS_URL" ) >"$TMP_LOGS/$host.log" 2>&1 &
        PIDS+=($!); HOSTS_BY_PID+=("$host")
        info "→ $host provisioning in background (log: $TMP_LOGS/$host.log)"
    done
    for j in "${!PIDS[@]}"; do
        if wait "${PIDS[$j]}"; then RESULTS+=("${HOSTS_BY_PID[$j]} ✓"); else RESULTS+=("${HOSTS_BY_PID[$j]} ✗"); RC=1; fi
    done
    log "Per-node output"
    for j in "${!PIDS[@]}"; do
        printf '\n=== %s ===\n' "${HOSTS_BY_PID[$j]}"
        cat "$TMP_LOGS/${HOSTS_BY_PID[$j]}.log" || true
    done
else
    for i in "${!HOSTS[@]}"; do
        idx=$((i + 1)); host="${HOSTS[$i]}"
        log "Node $idx / ${#HOSTS[@]} — $host"
        if provision_node "$idx" "$host" "$NATS_URL"; then
            RESULTS+=("$host ✓")
        else
            RESULTS+=("$host ✗"); RC=1
            warn "[$host] provisioning failed — continuing with the rest of the fleet"
        fi
    done
fi

log "Fleet summary"
for r in "${RESULTS[@]}"; do info "$r"; done
if [[ $RC -eq 0 ]]; then
    info "all nodes OK. Watch one with:  ssh ${SSH_USER}@<host> 'sudo journalctl -u turing -f'"
else
    warn "one or more nodes failed — see the per-node output above."
fi
exit $RC
