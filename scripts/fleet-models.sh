#!/usr/bin/env bash
# Distribute Ollama models across the Turing fleet from the coordinator.
#
# Every node runs its own Ollama and holds its own models; 8 GB Jetsons cannot
# all hold the same 7 B model *and* anything else, so the fleet is laid out
# deliberately — one node holds the coder, another the summariser — and the
# mesh routes local-tier requests to whichever node has the model
# (`turing.llm.pool`, advertised via presence heartbeats). This script is the
# placement half: it pulls the right models onto the right hosts, over SSH, in
# parallel, and shows what each node currently holds.
#
# Usage:
#   scripts/fleet-models.sh status  HOST...                      what each node has (+ loaded)
#   scripts/fleet-models.sh pull    --models "M1 M2" HOST...     pull M1 M2 on every HOST
#   scripts/fleet-models.sh plan    FILE                         pull per-host from a plan file
#   scripts/fleet-models.sh expose  --bind-address ADDR --i-understand-unauthenticated HOST...
#                                                               make each node's Ollama reachable
#   scripts/fleet-models.sh prune   --keep "M1 M2" HOST...       remove every model NOT in --keep
#
# Plan file format (`#` comments allowed; blank lines ignored):
#   jetson-1: qwen2.5:7b
#   jetson-2: qwen2.5:7b llama3.2:3b
#   jetson-3: gemma3:1b nomic-embed-text
#   jetson-4: llama3.2:3b
#
# Options (before the command or after it, either works):
#   --user USER      SSH user on each node (default: $TURING_FLEET_USER, else $USER)
#   --parallel       run hosts concurrently (per-host logs under a temp dir)
#   --keep-alive D   with `expose`: also set OLLAMA_KEEP_ALIVE=D (default 30m)
#   --dry-run        print the exact remote commands; run nothing
#   -h, --help       this help
#
# `expose` writes a systemd drop-in so ollama.service listens on the explicitly
# selected address at port 11434
# and keeps models resident (OLLAMA_KEEP_ALIVE), then restarts it. It is what
# makes `TURING_OLLAMA_ADVERTISE_HOST=http://<host>:11434` on that node true.
# Ollama has no auth, so only do this on the tailnet/LAN the fleet lives on —
# the same trust boundary NATS already relies on (nats_lan_only).
#
# Every remote command is idempotent: a second `pull` of a present model is a
# no-op on Ollama's side, `expose` rewrites the same drop-in, `prune` only
# removes what is not kept.

set -euo pipefail

USER_OPT="${TURING_FLEET_USER:-${USER:-}}"
PARALLEL=0
DRY_RUN=0
KEEP_ALIVE="30m"
BIND_ADDRESS=""
ACK_UNAUTHENTICATED=0
MODELS=""
KEEP=""
COMMAND=""
HOSTS=()
PLAN_FILE=""

usage() { sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) USER_OPT="${2:-}"; shift 2 ;;
        --parallel) PARALLEL=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --keep-alive) KEEP_ALIVE="${2:-}"; shift 2 ;;
        --bind-address) BIND_ADDRESS="${2:-}"; shift 2 ;;
        --i-understand-unauthenticated) ACK_UNAUTHENTICATED=1; shift ;;
        --models) MODELS="${2:-}"; shift 2 ;;
        --keep) KEEP="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        status|pull|plan|expose|prune)
            if [[ -n "$COMMAND" ]]; then
                echo "fleet-models: one command at a time (got '$COMMAND' and '$1')" >&2
                exit 2
            fi
            COMMAND="$1"; shift ;;
        -*)
            echo "fleet-models: unknown option '$1'" >&2; usage >&2; exit 2 ;;
        *)
            if [[ "$COMMAND" == "plan" && -z "$PLAN_FILE" ]]; then
                PLAN_FILE="$1"
            else
                HOSTS+=("$1")
            fi
            shift ;;
    esac
done

if [[ -z "$COMMAND" ]]; then
    usage >&2
    exit 2
fi

# Model names are `name[:tag]` — letters, digits, and a few separators. Anything
# else would be a shell-injection vector once interpolated into an ssh command,
# so refuse it here rather than quote our way around it.
validate_model() {
    if [[ ! "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*(:[A-Za-z0-9._-]+)?$ ]]; then
        echo "fleet-models: refusing model name '$1'" >&2
        exit 2
    fi
}
validate_host() {
    if [[ ! "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; then
        echo "fleet-models: refusing host '$1'" >&2
        exit 2
    fi
}
validate_keep_alive() {
    if [[ ! "$1" =~ ^(-1|[0-9]+(ms|s|m|h))$ ]]; then
        echo "fleet-models: refusing keep-alive '$1'" >&2
        exit 2
    fi
}
validate_bind_address() {
    # Hostnames, IPv4 literals, and bracketed IPv6 literals are safe to place
    # in the quoted systemd command.  The address is still an operator choice;
    # this validation only prevents shell syntax from crossing SSH.
    if [[ ! "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ && ! "$1" =~ ^\[[0-9A-Fa-f:]+\]$ ]]; then
        echo "fleet-models: refusing bind address '$1'" >&2
        exit 2
    fi
}

ssh_target() { if [[ -n "$USER_OPT" ]]; then echo "${USER_OPT}@$1"; else echo "$1"; fi; }

# run_remote HOST CMD — ssh in with a short connect timeout, batch mode (no
# password prompts hanging a parallel run), and the command as one string.
run_remote() {
    local host="$1" cmd="$2"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '[dry-run] ssh %s -- %s\n' "$(ssh_target "$host")" "$cmd"
        return 0
    fi
    ssh -o BatchMode=yes -o ConnectTimeout=8 "$(ssh_target "$host")" "$cmd"
}

pull_cmd() {
    # $@ = models. `ollama list` lookup first so a present model is a cheap
    # no-op even on Ollama versions that re-verify layers on every pull.
    local out="" m
    for m in "$@"; do
        out+="if ollama list 2>/dev/null | awk 'NR>1 {print \$1}' | grep -Fqx '$m'; then echo \"[$m] present\"; else echo \"[$m] pulling\"; ollama pull '$m'; fi; "
    done
    echo "$out"
}

status_cmd() {
    echo "echo '== models'; ollama list; echo '== loaded'; ollama ps 2>/dev/null || true"
}

expose_cmd() {
    # A drop-in rather than editing the unit: survives ollama upgrades.
    cat <<EOF
sudo mkdir -p /etc/systemd/system/ollama.service.d && \
printf '[Service]\nEnvironment=OLLAMA_HOST=${BIND_ADDRESS}:11434\nEnvironment=OLLAMA_KEEP_ALIVE=${KEEP_ALIVE}\n' | \
sudo tee /etc/systemd/system/ollama.service.d/10-turing-fleet.conf >/dev/null && \
sudo systemctl daemon-reload && sudo systemctl restart ollama && \
sleep 2 && curl -fsS http://127.0.0.1:11434/api/tags >/dev/null && echo 'ollama exposed on ${BIND_ADDRESS}:11434'
EOF
}

prune_cmd() {
    # $@ = models to keep. Removes everything else this node holds.
    local keep_words="" m
    for m in "$@"; do keep_words+="${m}\\n"; done
    echo "for m in \$(ollama list 2>/dev/null | awk 'NR>1 {print \$1}'); do if ! printf '$keep_words' | grep -Fqx \"\$m\"; then echo \"[\$m] removing\"; ollama rm \"\$m\"; else echo \"[\$m] kept\"; fi; done"
}

# run_on_hosts CMD_BUILDER HOST... — the builder receives the host and prints
# the remote command; hosts run sequentially or in parallel.
run_on_hosts() {
    local builder="$1"; shift
    local hosts=("$@") host pids=() logdir="" rc=0
    if [[ ${#hosts[@]} -eq 0 ]]; then
        echo "fleet-models: no hosts given" >&2
        exit 2
    fi
    for host in "${hosts[@]}"; do validate_host "$host"; done
    if [[ "$PARALLEL" -eq 1 && "$DRY_RUN" -eq 0 ]]; then
        logdir="$(mktemp -d "${TMPDIR:-/tmp}/fleet-models.XXXXXX")"
        for host in "${hosts[@]}"; do
            ( run_remote "$host" "$("$builder" "$host")" >"$logdir/$host.log" 2>&1 ) &
            pids+=("$!")
        done
        local i=0
        for host in "${hosts[@]}"; do
            if ! wait "${pids[$i]}"; then rc=1; fi
            echo "── $host"; cat "$logdir/$host.log"
            i=$((i + 1))
        done
        return "$rc"
    fi
    for host in "${hosts[@]}"; do
        echo "── $host"
        run_remote "$host" "$("$builder" "$host")" || rc=1
    done
    return "$rc"
}

case "$COMMAND" in
    status)
        build() { status_cmd; }
        # `${arr[@]+"${arr[@]}"}`: an empty array is "unbound" under `set -u`
        # on the macOS bash 3.2 this runs from; the guard expands to nothing.
        run_on_hosts build ${HOSTS[@]+"${HOSTS[@]}"}
        ;;
    pull)
        if [[ -z "$MODELS" ]]; then echo "fleet-models: pull needs --models" >&2; exit 2; fi
        read -r -a MODEL_LIST <<<"$MODELS"
        for m in "${MODEL_LIST[@]}"; do validate_model "$m"; done
        build() { pull_cmd "${MODEL_LIST[@]}"; }
        # `${arr[@]+"${arr[@]}"}`: an empty array is "unbound" under `set -u`
        # on the macOS bash 3.2 this runs from; the guard expands to nothing.
        run_on_hosts build ${HOSTS[@]+"${HOSTS[@]}"}
        ;;
    prune)
        if [[ -z "$KEEP" ]]; then echo "fleet-models: prune needs --keep (an empty keep list would wipe every node)" >&2; exit 2; fi
        read -r -a KEEP_LIST <<<"$KEEP"
        for m in "${KEEP_LIST[@]}"; do validate_model "$m"; done
        build() { prune_cmd "${KEEP_LIST[@]}"; }
        # `${arr[@]+"${arr[@]}"}`: an empty array is "unbound" under `set -u`
        # on the macOS bash 3.2 this runs from; the guard expands to nothing.
        run_on_hosts build ${HOSTS[@]+"${HOSTS[@]}"}
        ;;
    expose)
        if [[ -z "$BIND_ADDRESS" ]]; then
            echo "fleet-models: expose requires --bind-address (refusing an implicit public bind)" >&2
            exit 2
        fi
        if [[ "$ACK_UNAUTHENTICATED" -ne 1 ]]; then
            echo "fleet-models: expose requires --i-understand-unauthenticated" >&2
            exit 2
        fi
        validate_bind_address "$BIND_ADDRESS"
        validate_keep_alive "$KEEP_ALIVE"
        build() { expose_cmd; }
        # `${arr[@]+"${arr[@]}"}`: an empty array is "unbound" under `set -u`
        # on the macOS bash 3.2 this runs from; the guard expands to nothing.
        run_on_hosts build ${HOSTS[@]+"${HOSTS[@]}"}
        ;;
    plan)
        if [[ -z "$PLAN_FILE" || ! -f "$PLAN_FILE" ]]; then
            echo "fleet-models: plan needs a readable plan file" >&2; exit 2
        fi
        # Two parallel indexed arrays rather than an associative one: the
        # coordinator may be this Mac, whose /bin/bash is 3.2 (no `declare -A`).
        PLAN_HOSTS=()
        PLAN_MODELS=()
        while IFS= read -r line || [[ -n "$line" ]]; do
            line="${line%%#*}"
            [[ -z "${line// /}" ]] && continue
            host="${line%%:*}"; models="${line#*:}"
            host="${host// /}"
            validate_host "$host"
            read -r -a ms <<<"$models"
            for m in ${ms[@]+"${ms[@]}"}; do validate_model "$m"; done
            PLAN_HOSTS+=("$host")
            PLAN_MODELS+=("${ms[*]-}")
        done <"$PLAN_FILE"
        if [[ ${#PLAN_HOSTS[@]} -eq 0 ]]; then
            echo "fleet-models: plan file has no host lines" >&2; exit 2
        fi
        plan_models_for() {
            local i=0 h
            for h in "${PLAN_HOSTS[@]}"; do
                if [[ "$h" == "$1" ]]; then echo "${PLAN_MODELS[$i]}"; return 0; fi
                i=$((i + 1))
            done
        }
        echo "plan:"
        i=0
        for host in "${PLAN_HOSTS[@]}"; do
            printf '  %-14s %s\n' "$host" "${PLAN_MODELS[$i]}"; i=$((i + 1))
        done
        build() { read -r -a ms <<<"$(plan_models_for "$1")"; pull_cmd ${ms[@]+"${ms[@]}"}; }
        run_on_hosts build "${PLAN_HOSTS[@]}"
        ;;
esac
