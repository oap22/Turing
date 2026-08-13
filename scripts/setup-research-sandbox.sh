#!/usr/bin/env bash
# Turing research sandbox — provision the `turing` macOS user that owns
# ~turing/turing-workspace (ADR 0011 §8).
#
#   bash scripts/setup-research-sandbox.sh            # PLAN ONLY — prints, changes nothing
#   bash scripts/setup-research-sandbox.sh --verify   # boundary assertions only, read-only
#   bash scripts/setup-research-sandbox.sh --apply    # actually do it (asks for confirmation)
#   bash scripts/setup-research-sandbox.sh --apply --yes   # ...without the prompt
#
# ── THIS SCRIPT DOES NOTHING UNLESS YOU PASS --apply ────────────────────────
# Invoked with no flags it runs read-only discovery (dscl -list, stat, id) and
# prints the exact commands it *would* run, in order. That is deliberate: the
# steps below create a user account and tighten the permissions on your own home
# directory, and neither should happen as a side effect of someone running a
# script to see what it does. Every step is idempotent — re-running after a
# partial apply skips what is already in place.
#
# ── WHY A SEPARATE USER AND NOT JUST A DIRECTORY ────────────────────────────
# Turing's shell gate constrains commands *Turing* runs. But the research
# agent's job is authoring Python and executing it, and a Python process writes
# anywhere its user can write, regardless of cwd. `cd ~/turing-workspace` does
# not stop `shutil.rmtree(base_dir)` when `base_dir` came back empty.
#
#   A directory is a convention. A user account is a boundary the OS enforces.
#
# The realistic failure this defends against is not malice. It is:
#
#   1. `rmtree` on an empty path variable — the classic. `rm -rf "$PREFIX/"`
#      with PREFIX unset deletes whatever the *user* can reach, which is why
#      the answer is to shrink what the user can reach.
#   2. A run filling the disk unattended at 3am. Nothing here caps that; see
#      the APFS-quota appendix printed at the end, which is an option, not a
#      decision — the brief names the failure and does not name a mitigation.
#
# The `turing` user must have no reach into the operator's vault, SSH keys, MCP
# credentials, or Claude credentials. Step 5 tightens the operator home so that
# is true, and --verify asserts it afterwards rather than assuming it.
#
# ── WHAT THIS SCRIPT DOES *NOT* DO, AND WHY --verify SAYS SO ────────────────
# Provisioning the sandbox is not the same as the loop using it. Two gaps are
# real today, both asserted by --verify rather than assumed away:
#
#   1. `ResearchLoopSettings.research_workspace_root` defaults to
#      `~/turing-workspace` — the *operator's* home when the loop runs as the
#      operator. Unless TURING_RESEARCH_WORKSPACE_ROOT points at the sandbox
#      workspace below, this script provisions a boundary nothing goes through.
#   2. Nothing in `src/turing/research/` drops privileges to `turing`.
#      Pointing the workspace root at a turing-owned 700 directory is necessary
#      but not sufficient: an operator-owned process cannot write there, and an
#      operator-owned process that *could* write there would still be running
#      as the operator. The loop must be launched under the sandbox user, and
#      the wiring for that is not written yet (ADR 0011 § 8, brief Q11).
#
# --verify therefore reports "the sandbox is sealed", never "the agent is
# inside it". Those are different claims and only the first one is checkable
# from here.
#
# ── STEP 5 TIGHTENS THE OPERATOR HOME, WHICH HIDES THE SCAFFOLD FORK ────────
# Mode 750 on the operator home means a process running as turing
# cannot traverse it — including `~/Developer/active/turing-skills`, the
# default destination of scripts/bootstrap-turing-skills.sh. The agent's own
# writable scaffold would be unreachable to the agent. Step 7 provisions a
# shared exchange directory outside both homes and --verify asserts the fork is
# reachable from it; the fork must be created there with
#   TURING_SKILLS_DEST=<exchange>/turing-skills bash scripts/bootstrap-turing-skills.sh …
#
# Source of record: research/briefs/2026-08-12-autonomous-research-agent.md
#                   § Execution environment.
# Decision record:  docs/adr/0011-autonomous-research-agent-retarget.md § 8.

set -euo pipefail

# ── tunables ─────────────────────────────────────────────────────────
SANDBOX_USER="turing"
SANDBOX_GROUP="turing"
SANDBOX_REALNAME="Turing research sandbox"
SANDBOX_HOME="/Users/${SANDBOX_USER}"
SANDBOX_WORKSPACE="${SANDBOX_HOME}/turing-workspace"
SANDBOX_SHELL="/bin/zsh"

# The env var that points the loop at the workspace above. Without it the loop
# writes to the *operator's* ~/turing-workspace and the boundary is decoration.
WORKSPACE_ENV_VAR="TURING_RESEARCH_WORKSPACE_ROOT"

# Neither home is reachable from the other once step 5 runs, so anything both
# the operator and the sandbox user need — the turing-skills fork above all —
# lives here instead. Group-owned by ${SANDBOX_GROUP}, setgid so new files
# inherit it, with the operator added to that group in step 7.
EXCHANGE_DIR="/Users/Shared/turing"
EXCHANGE_FORK="${EXCHANGE_DIR}/turing-skills"
FORK_ENV_VAR="TURING_SKILLS_DEST"

# First UID/GID considered. 500–999 is the conventional macOS band for service
# accounts that are not meant to appear at the login window; 501+ is where real
# human accounts start, so we search upward from 550 for a free slot.
UID_SEARCH_START=550
GID_SEARCH_START=550

# Paths the sandbox user must NOT be able to read. Relative to the operator
# home unless absolute. TURING_OPERATOR_VAULT is appended when set, because the
# vault location is operator-specific and the brief does not fix it.
declare -a FORBIDDEN_PATHS=(
    ".ssh"
    ".ssh/id_ed25519"
    ".ssh/id_rsa"
    ".claude"
    ".claude.json"
    ".mcp.json"
    ".config/gh"
    "Library/Keychains"
    "Library/Application Support/Claude"
)

# ── helpers ──────────────────────────────────────────────────────────
log()  { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }
step() { printf '\n\033[1;36m── %s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

MODE="plan"
ASSUME_YES=0

usage() {
    sed -n '2,69p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)  MODE="apply"  ;;
        --verify) MODE="verify" ;;
        --plan)   MODE="plan"   ;;
        --yes|-y) ASSUME_YES=1  ;;
        -h|--help) usage 0 ;;
        *) warn "unknown argument: $1"; usage 2 ;;
    esac
    shift
done

require_macos() {
    [[ "$(uname -s)" == "Darwin" ]] \
        || die "macOS only — this provisions a local directory-service user via dscl (got $(uname -s))."
}

require_not_root() {
    [[ $EUID -ne 0 ]] \
        || die "Run as your own sudo-capable account, not root. The script sudos where it needs to."
}

# Emit one planned command, as a single string. In plan mode it is printed; in
# apply mode it is printed and then run. Nothing else in this script mutates
# state, so this is the single choke point between "describe" and "do".
run_step() {
    printf '    $ %s\n' "$1"
    if [[ "$MODE" == "apply" ]]; then
        eval "$1"
    fi
}

# Idempotence guard: announce a step as already satisfied.
already() { printf '    \033[1;32m(already done — skipped)\033[0m %s\n' "$*"; }

user_exists()  { dscl . -read "/Users/${SANDBOX_USER}" >/dev/null 2>&1; }
group_exists() { dscl . -read "/Groups/${SANDBOX_GROUP}" >/dev/null 2>&1; }

# Lowest free id at or above $1 in the given dscl category/attribute.
first_free_id() {
    local start="$1" path="$2" attr="$3" used candidate
    used="$(dscl . -list "$path" "$attr" 2>/dev/null | awk '{print $2}' | sort -n | uniq)"
    candidate="$start"
    while printf '%s\n' "$used" | grep -qx "$candidate"; do
        candidate=$((candidate + 1))
    done
    printf '%s' "$candidate"
}

# ── preflight (read-only in every mode) ──────────────────────────────
require_macos
require_not_root

REPO_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"

OPERATOR_USER="$(id -un)"
OPERATOR_HOME="$(dscl . -read "/Users/${OPERATOR_USER}" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
[[ -n "$OPERATOR_HOME" && -d "$OPERATOR_HOME" ]] \
    || die "Could not resolve the home directory for '${OPERATOR_USER}'."

[[ -n "${TURING_OPERATOR_VAULT:-}" ]] && FORBIDDEN_PATHS+=("${TURING_OPERATOR_VAULT}")

# True when $1 is an absolute path that does not sit under the operator home,
# i.e. a path step 5's chmod cannot shield. Those need their own mode change.
outside_operator_home() {
    case "$1" in
        "${OPERATOR_HOME}"/*) return 1 ;;
        /*)                   return 0 ;;
        *)                    return 1 ;;
    esac
}

# The loop's configured workspace root, as the loop itself would resolve it:
# the process environment first, then a repo .env, then unset. Deliberately
# not the pydantic default — the default is the failure this checks for.
configured_workspace_root() {
    local from_env
    from_env="${!WORKSPACE_ENV_VAR:-}"
    if [[ -n "$from_env" ]]; then
        printf '%s' "$from_env"
        return 0
    fi
    if [[ -f "${REPO_ROOT}/.env" ]]; then
        sed -n "s/^[[:space:]]*${WORKSPACE_ENV_VAR}[[:space:]]*=[[:space:]]*//p" \
            "${REPO_ROOT}/.env" | tail -n 1 | tr -d '"'"'"'' | tr -d '\r'
    fi
}

if user_exists; then
    SANDBOX_UID="$(dscl . -read "/Users/${SANDBOX_USER}" UniqueID | awk '{print $2}')"
else
    SANDBOX_UID="$(first_free_id "$UID_SEARCH_START" /Users UniqueID)"
fi
if group_exists; then
    SANDBOX_GID="$(dscl . -read "/Groups/${SANDBOX_GROUP}" PrimaryGroupID | awk '{print $2}')"
else
    SANDBOX_GID="$(first_free_id "$GID_SEARCH_START" /Groups PrimaryGroupID)"
fi

OPERATOR_HOME_MODE="$(stat -f '%Lp' "$OPERATOR_HOME")"

# ── verify mode: assert the boundary, change nothing ─────────────────
if [[ "$MODE" == "verify" ]]; then
    log "Verifying the sandbox boundary (read-only)"

    user_exists || die "User '${SANDBOX_USER}' does not exist. Run this script with --apply first."

    failures=0

    step "The sandbox user can write its own workspace"
    if sudo -u "$SANDBOX_USER" test -w "$SANDBOX_WORKSPACE" 2>/dev/null; then
        ok "writable: ${SANDBOX_WORKSPACE}"
    else
        warn "NOT writable by ${SANDBOX_USER}: ${SANDBOX_WORKSPACE}"
        failures=$((failures + 1))
    fi

    step "The sandbox user cannot reach operator secrets"
    for rel in "${FORBIDDEN_PATHS[@]}"; do
        if [[ "$rel" == /* ]]; then target="$rel"; else target="${OPERATOR_HOME}/${rel}"; fi
        if [[ ! -e "$target" ]]; then
            info "absent on this machine, nothing to protect: ${target}"
            continue
        fi
        # `test -r` as the sandbox user is the honest question: can it open it?
        if sudo -u "$SANDBOX_USER" test -r "$target" 2>/dev/null; then
            warn "READABLE by ${SANDBOX_USER}: ${target}"
            if outside_operator_home "$target"; then
                warn "  It sits outside ${OPERATOR_HOME}, so step 5's chmod does not cover it."
                warn "  remedy: sudo chmod -R o-rwx '${target}'   (or move it under the operator home)"
            else
                warn "  remedy: re-run with --apply; step 5 tightens ${OPERATOR_HOME} to mode 750."
            fi
            failures=$((failures + 1))
        else
            ok "unreadable: ${target}"
        fi
    done

    step "The sandbox user is not an admin"
    if dsmemberutil checkmembership -U "$SANDBOX_USER" -G admin 2>/dev/null | grep -q 'is a member'; then
        warn "${SANDBOX_USER} IS in the admin group — it can sudo, which voids the boundary"
        failures=$((failures + 1))
    else
        ok "${SANDBOX_USER} is not in the admin group"
    fi

    # ── the checks that catch a sandbox provisioned and then never used ──
    step "The loop is pointed at the sandbox workspace"
    info "A sandbox nothing writes into is not a boundary. The loop's workspace root"
    info "defaults to \$HOME/turing-workspace, which is the OPERATOR's home when the"
    info "loop runs as the operator — so ${WORKSPACE_ENV_VAR} must be set."
    CONFIGURED_ROOT="$(configured_workspace_root)"
    if [[ -z "$CONFIGURED_ROOT" ]]; then
        warn "${WORKSPACE_ENV_VAR} is not set (checked the environment and ${REPO_ROOT}/.env)."
        warn "  remedy: echo '${WORKSPACE_ENV_VAR}=${SANDBOX_WORKSPACE}' >> ${REPO_ROOT}/.env"
        failures=$((failures + 1))
    elif [[ "$CONFIGURED_ROOT" != "$SANDBOX_WORKSPACE" ]]; then
        warn "${WORKSPACE_ENV_VAR}=${CONFIGURED_ROOT}, not ${SANDBOX_WORKSPACE}."
        warn "  The agent would work outside the sandbox this script provisioned."
        failures=$((failures + 1))
    else
        ok "${WORKSPACE_ENV_VAR}=${CONFIGURED_ROOT}"
    fi

    step "No unconfined workspace was left in the operator home"
    if [[ -e "${OPERATOR_HOME}/turing-workspace" ]]; then
        warn "PRESENT: ${OPERATOR_HOME}/turing-workspace"
        warn "  The loop has run (or is configured to run) outside the sandbox. Inspect it"
        warn "  before deleting: anything in there was produced by an unconfined agent."
        failures=$((failures + 1))
    else
        ok "absent: ${OPERATOR_HOME}/turing-workspace"
    fi

    step "The scaffold fork is reachable by ${SANDBOX_USER}"
    info "Step 5 puts the operator home at mode 750, so a fork under it is invisible"
    info "to the sandbox user — the agent's own writable scaffold would be unreachable."
    FORK_PATH="${!FORK_ENV_VAR:-${EXCHANGE_FORK}}"
    if [[ ! -d "${FORK_PATH}/.git" ]]; then
        info "no fork at ${FORK_PATH} yet — create it with:"
        info "  ${FORK_ENV_VAR}=${EXCHANGE_FORK} bash scripts/bootstrap-turing-skills.sh --apply"
    elif outside_operator_home "$FORK_PATH" \
        && sudo -u "$SANDBOX_USER" test -w "$FORK_PATH" 2>/dev/null; then
        ok "writable by ${SANDBOX_USER}: ${FORK_PATH}"
    else
        warn "NOT writable by ${SANDBOX_USER}: ${FORK_PATH}"
        warn "  remedy: move the fork under ${EXCHANGE_DIR} and re-run --apply, or set"
        warn "  ${FORK_ENV_VAR}=${EXCHANGE_FORK} before bootstrap-turing-skills.sh."
        failures=$((failures + 1))
    fi

    echo
    if [[ "$failures" -eq 0 ]]; then
        ok "Sandbox sealed — ${#FORBIDDEN_PATHS[@]} secret paths checked, workspace wiring confirmed."
        echo
        warn "This asserts the sandbox is SEALED. It does not assert the agent runs INSIDE it."
        warn "Nothing in src/turing/research/ drops privileges to ${SANDBOX_USER}: the loop must"
        warn "be launched under that user, and that wiring is not written yet (ADR 0011 § 8)."
        exit 0
    fi
    die "${failures} boundary check(s) failed. Do not run the agent until these are clean."
fi

# ── the plan ─────────────────────────────────────────────────────────
# Note: no bash-4 syntax anywhere in this script — stock macOS ships bash 3.2.
CONFIGURED_ROOT_PREVIEW="$(configured_workspace_root)"
[[ -z "$CONFIGURED_ROOT_PREVIEW" ]] \
    && CONFIGURED_ROOT_PREVIEW="<unset — the loop would use \$HOME/turing-workspace; see step 8>"

log "Turing research sandbox — ${MODE} mode"
cat <<EOF
  operator         : ${OPERATOR_USER} (home ${OPERATOR_HOME}, mode ${OPERATOR_HOME_MODE})
  sandbox user     : ${SANDBOX_USER} (uid ${SANDBOX_UID}, gid ${SANDBOX_GID})
  sandbox home     : ${SANDBOX_HOME}
  agent workspace  : ${SANDBOX_WORKSPACE}
  loop points here : ${WORKSPACE_ENV_VAR}=${CONFIGURED_ROOT_PREVIEW}
  exchange dir     : ${EXCHANGE_DIR} (scaffold fork: ${EXCHANGE_FORK})
  vault to shield  : ${TURING_OPERATOR_VAULT:-<not set — export TURING_OPERATOR_VAULT to include it>}
EOF

if [[ "$MODE" == "plan" ]]; then
    cat <<'EOF'

  PLAN ONLY. Nothing below has been executed. Read it, then re-run with
  --apply to carry it out, and --verify afterwards to assert the boundary.
EOF
fi

if [[ "$MODE" == "apply" && "$ASSUME_YES" -eq 0 ]]; then
    echo
    warn "This will create a local user account and tighten permissions on ${OPERATOR_HOME}."
    read -r -p "Type 'apply' to proceed: " CONFIRM || CONFIRM=""
    [[ "$CONFIRM" == "apply" ]] || die "Not confirmed — nothing was changed."
fi

step "1. Group '${SANDBOX_GROUP}' (gid ${SANDBOX_GID})"
info "A dedicated primary group, not 'staff'. Files in the operator home that are"
info "group-readable by staff stay unreadable to the sandbox user."
if group_exists; then
    already "/Groups/${SANDBOX_GROUP}"
else
    run_step "sudo dseditgroup -o create -i ${SANDBOX_GID} -r '${SANDBOX_REALNAME}' -t group ${SANDBOX_GROUP}"
fi

step "2. User '${SANDBOX_USER}' (uid ${SANDBOX_UID})"
info "Password is disabled ('*'): there is no interactive login and no way to"
info "authenticate as this user. The operator reaches it only via 'sudo -u ${SANDBOX_USER}'."
info "IsHidden keeps it off the login window. It is NOT added to 'admin' — an"
info "account that can sudo is not a boundary."
if user_exists; then
    already "/Users/${SANDBOX_USER}"
else
    run_step "sudo dscl . -create /Users/${SANDBOX_USER}"
    run_step "sudo dscl . -create /Users/${SANDBOX_USER} UserShell ${SANDBOX_SHELL}"
    run_step "sudo dscl . -create /Users/${SANDBOX_USER} RealName '${SANDBOX_REALNAME}'"
    run_step "sudo dscl . -create /Users/${SANDBOX_USER} UniqueID ${SANDBOX_UID}"
    run_step "sudo dscl . -create /Users/${SANDBOX_USER} PrimaryGroupID ${SANDBOX_GID}"
    run_step "sudo dscl . -create /Users/${SANDBOX_USER} NFSHomeDirectory ${SANDBOX_HOME}"
    run_step "sudo dscl . -create /Users/${SANDBOX_USER} IsHidden 1"
    run_step "sudo dscl . -create /Users/${SANDBOX_USER} Password '*'"
fi

step "3. Sandbox home ${SANDBOX_HOME} (mode 700)"
if [[ -d "$SANDBOX_HOME" ]]; then
    already "${SANDBOX_HOME} exists"
else
    run_step "sudo mkdir -p ${SANDBOX_HOME}"
fi
run_step "sudo chown -R ${SANDBOX_USER}:${SANDBOX_GROUP} ${SANDBOX_HOME}"
run_step "sudo chmod 700 ${SANDBOX_HOME}"

step "4. Agent workspace ${SANDBOX_WORKSPACE} (mode 700)"
info "One fresh subdirectory per project: ${SANDBOX_WORKSPACE}/<project-id>/."
info "The solver creates those; this script only creates the parent."
if [[ -d "$SANDBOX_WORKSPACE" ]]; then
    already "${SANDBOX_WORKSPACE} exists"
else
    run_step "sudo -u ${SANDBOX_USER} mkdir -p ${SANDBOX_WORKSPACE}"
fi
run_step "sudo chmod 700 ${SANDBOX_WORKSPACE}"

step "5. Tighten the operator home ${OPERATOR_HOME} (currently mode ${OPERATOR_HOME_MODE})"
info "This is the step that actually creates the boundary. Without it the sandbox"
info "user can traverse the operator home and read anything world-readable inside"
info "it — ~/Developer, ~/Documents, and any repo checked out there."
info "The .ssh and Library directories are already mode 700 by default; the rest are not."
if [[ "$OPERATOR_HOME_MODE" == "700" || "$OPERATOR_HOME_MODE" == "750" ]]; then
    already "mode ${OPERATOR_HOME_MODE} already excludes 'other'"
else
    warn "mode ${OPERATOR_HOME_MODE} allows other users to traverse ${OPERATOR_HOME}"
    run_step "sudo chmod 750 ${OPERATOR_HOME}"
fi

step "6. Shield a vault that lives outside the operator home"
info "Step 5 only covers paths under ${OPERATOR_HOME}. A vault kept elsewhere —"
info "an external volume, /Users/Shared, an iCloud path — needs its own mode change,"
info "or --verify reports it READABLE with nothing above having tried to fix it."
if [[ -z "${TURING_OPERATOR_VAULT:-}" ]]; then
    info "TURING_OPERATOR_VAULT is not set — nothing to shield. Export it and re-run"
    info "if the vault is not under ${OPERATOR_HOME}."
elif ! outside_operator_home "${TURING_OPERATOR_VAULT}"; then
    already "${TURING_OPERATOR_VAULT} is under ${OPERATOR_HOME} — covered by step 5"
elif [[ ! -e "${TURING_OPERATOR_VAULT}" ]]; then
    warn "TURING_OPERATOR_VAULT=${TURING_OPERATOR_VAULT} does not exist — check the path."
else
    run_step "sudo chmod -R o-rwx '${TURING_OPERATOR_VAULT}'"
fi

step "7. Shared exchange directory ${EXCHANGE_DIR} (setgid ${SANDBOX_GROUP})"
info "After step 5 the two homes are mutually unreachable. The turing-skills fork is"
info "the agent's own writable scaffold and must be reachable from both: the agent"
info "commits to it, the operator inspects it and records its SHA per round. Under"
info "${OPERATOR_HOME} it is invisible to ${SANDBOX_USER}; under ${SANDBOX_HOME} (mode 700)"
info "it is invisible to the operator. It goes here instead."
info "Mode 2770: owned by ${SANDBOX_USER}, group ${SANDBOX_GROUP} with the operator added"
info "to that group, setgid so files created by either side stay group-shared. The"
info "operator joining '${SANDBOX_GROUP}' does not widen the sandbox's reach — the"
info "operator's own files are group 'staff', which ${SANDBOX_USER} is not in."
if [[ -d "$EXCHANGE_DIR" ]]; then
    already "${EXCHANGE_DIR} exists"
else
    run_step "sudo mkdir -p ${EXCHANGE_DIR}"
fi
run_step "sudo dseditgroup -o edit -a ${OPERATOR_USER} -t user ${SANDBOX_GROUP}"
run_step "sudo chown -R ${SANDBOX_USER}:${SANDBOX_GROUP} ${EXCHANGE_DIR}"
run_step "sudo chmod -R 2770 ${EXCHANGE_DIR}"
info "Create the fork there — the bootstrap script's default destination is under"
info "the operator home and would be unreachable to the agent:"
info "  ${FORK_ENV_VAR}=${EXCHANGE_FORK} bash scripts/bootstrap-turing-skills.sh --apply"
info "It clones as the operator, so hand it back afterwards:"
info "  sudo chown -R ${SANDBOX_USER}:${SANDBOX_GROUP} ${EXCHANGE_FORK} && sudo chmod -R g+w ${EXCHANGE_FORK}"

step "8. Point the loop at the sandbox workspace"
info "This is the step that turns the boundary from provisioned into used."
info "\`ResearchLoopSettings.research_workspace_root\` defaults to \$HOME/turing-workspace,"
info "which resolves inside the OPERATOR's home when the loop runs as the operator."
info "Nothing is changed automatically here: .env is the operator's file."
printf '    $ %s\n' "echo '${WORKSPACE_ENV_VAR}=${SANDBOX_WORKSPACE}' >> ${REPO_ROOT}/.env"
warn "Setting it is necessary and NOT sufficient. ${SANDBOX_WORKSPACE} is mode 700 and"
warn "owned by ${SANDBOX_USER}: an operator-owned loop process cannot write there, and"
warn "one that could would still be running as the operator. The loop has to be"
warn "launched under ${SANDBOX_USER}, and that wiring does not exist yet — no code under"
warn "src/turing/research/ drops privileges. Until it does, this account is a"
warn "provisioned boundary the loop does not go through (ADR 0011 § 8, brief Q11)."

step "9. Verify"
info "Run the boundary assertions. They are read-only and safe to repeat."
printf '    $ %s\n' "bash scripts/setup-research-sandbox.sh --verify"
if [[ "$MODE" == "apply" ]]; then
    echo
    warn "Re-run with --verify now. Do not start the agent until it is clean."
fi

# ── appendix: the unattended disk-fill failure ───────────────────────
cat <<'EOF'

── APPENDIX — the other realistic failure, and an option that is NOT decided ──

A user account bounds *where* the agent can write. It does not bound *how much*.
The second failure named in the brief is "a training run filling the disk
unattended at 3am", and nothing above prevents it.

The brief names the failure; it does not name a mitigation. This is therefore an
option to consider, not part of the plan, and it is not executed by this script
in any mode:

  # Find the APFS container backing the boot volume:
  diskutil list

  # Create a quota-capped volume in that container, mounted as the workspace.
  # Check the flag exists on your macOS build first: `diskutil apfs addVolume -h`
  sudo diskutil apfs addVolume <containerDisk> APFS TuringWorkspace \
      -quota 100g -mountpoint /Users/turing/turing-workspace

Trade-offs to weigh before adopting it: an APFS volume is a mount point, so the
sandbox home and the workspace end up on different filesystems (hardlinks and
atomic renames across them fail); the quota is a hard wall, so a run that hits it
fails in a way the solver must treat as a harness failure rather than a bad
solution; and it is one more thing to reprovision on a new machine.

The cheaper alternative, also undecided: leave the disk unbounded and rely on the
per-project wall-clock cap (ADR 0011 §5) to bound how long a runaway run has to
fill it. That is weaker — a cap in hours still permits a lot of gigabytes — but
it needs no filesystem surgery.

EOF

if [[ "$MODE" == "plan" ]]; then
    log "Plan complete — nothing was changed. Re-run with --apply to carry it out."
else
    log "Apply complete. Now run: bash scripts/setup-research-sandbox.sh --verify"
fi
