#!/usr/bin/env bash
# Fork the operator's skills repo into `turing-skills` at a frozen commit —
# the agent's writable scaffold (ADR 0011 §6).
#
#   bash scripts/bootstrap-turing-skills.sh            # PLAN ONLY — prints, changes nothing
#   bash scripts/bootstrap-turing-skills.sh --verify   # assert an existing fork, read-only
#   bash scripts/bootstrap-turing-skills.sh --apply    # actually do it (asks for confirmation)
#   bash scripts/bootstrap-turing-skills.sh --apply --yes
#
# Options:
#   --source <path>   source repo            (default ~/Developer/active/skills)
#   --dest <path>     fork destination       (default ~/Developer/active/turing-skills)
#   --commit <sha>    freeze at this commit  (default: the source's current HEAD)
#
# ── THIS SCRIPT DOES NOTHING UNLESS YOU PASS --apply ────────────────────────
# Invoked with no flags it reads the source repo (rev-parse, status, ls-tree)
# and prints the exact commands it would run. It is idempotent: a second --apply
# against an existing fork refuses rather than clobbering it, because a fork
# recreated from a *later* source commit is a different scaffold baseline and
# would silently restart the trajectory.
#
# ── LOCAL ONLY. NO REMOTE IS CREATED, AND `origin` IS REMOVED ───────────────
# `git clone` sets `origin` to the source repo. That is left in place by
# accident in most fork scripts and is exactly wrong here: the agent commits
# freely to this repo, and an inherited `origin` points its pushes at the
# operator's personal skills repo. Step 3 removes it. Publishing the fork — to
# GitHub or anywhere else — is an operator decision, taken later, by hand.
#
# ── WHY THE FORK EXISTS ─────────────────────────────────────────────────────
# The scaffold *is* skills: self-edits land as skill files, a git SHA is the
# scaffold version per round, `git revert` is rollback, and a diff is the answer
# to "what changed". Two independent problems make a shared skills repo
# unworkable, and either alone would justify the fork:
#
#   1. The measurement layer lives in the skills repo. `research-loop/`
#      (SKILL.md, conventions.md, driving-functions.md, log_run.py) is the
#      ruler this program is measured with. An agent with write access to its
#      own ruler is not measured — it can widen the tolerance, redefine the
#      noise floor, or rewrite what counts as a round. Step 4 therefore does not
#      merely mark it read-only inside the fork; it *removes* it, because the
#      agent commits freely here and "read-only" inside a repo it owns is a
#      convention with nothing enforcing it. The canonical copy stays in the
#      source repo, outside the agent's write surface.
#   2. The operator's own `skillify` edits would confound the trajectory. Daily
#      edits to a shared repo land inside the scaffold between rounds and show
#      up in the delta as scaffold improvement. That is the same confound as
#      free-form escalation advice, arriving through another door.
#
# Accepted cost, recorded in the brief: the fork diverges from the personal
# skills repo, and no merge-back is planned.
#
# ── WHAT IS NOT SOLVED HERE ─────────────────────────────────────────────────
# Removing the measurement layer from the fork does not make the *source* repo
# unreachable — the agent's user could still read it if filesystem permissions
# allow. How harness and measurement read-only-ness is actually enforced
# (separate process, container, mount) is brief question Q11 and a loop-2
# blocker; see scripts/setup-research-sandbox.sh for the user-account half of
# the answer, which is the only half that exists today.
#
# Source of record: research/briefs/2026-08-12-autonomous-research-agent.md
#                   § Settled decisions (Scaffold repo), § Multi-agent workflow boundary.
# Decision record:  docs/adr/0011-autonomous-research-agent-retarget.md § 6.

set -euo pipefail

# ── tunables ─────────────────────────────────────────────────────────
SOURCE_REPO="${TURING_SKILLS_SOURCE:-${HOME}/Developer/active/skills}"
DEST_REPO="${TURING_SKILLS_DEST:-${HOME}/Developer/active/turing-skills}"
FREEZE_COMMIT=""
FORK_TAG="fork-point"
PROVENANCE_FILE="FORK-PROVENANCE.md"

# Paths removed from the fork: the measurement layer. Everything else in the
# source repo — the operator's daily skills — is carried over, because the
# secondary Goodhart axis measures whether the evolved scaffold still does
# daily work, and it cannot measure that against skills the fork never had.
MEASUREMENT_PATHS="skills/research-loop"

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
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)  MODE="apply"  ;;
        --verify) MODE="verify" ;;
        --plan)   MODE="plan"   ;;
        --yes|-y) ASSUME_YES=1  ;;
        --source) shift; SOURCE_REPO="${1:?--source needs a path}" ;;
        --dest)   shift; DEST_REPO="${1:?--dest needs a path}" ;;
        --commit) shift; FREEZE_COMMIT="${1:?--commit needs a sha}" ;;
        -h|--help) usage 0 ;;
        *) warn "unknown argument: $1"; usage 2 ;;
    esac
    shift
done

# The single choke point between "describe" and "do". Takes one command string.
run_step() {
    printf '    $ %s\n' "$1"
    if [ "$MODE" = "apply" ]; then
        eval "$1"
    fi
}

# ── preflight (read-only in every mode) ──────────────────────────────
command -v git >/dev/null 2>&1 || die "git not found on PATH."

if [ "$MODE" = "verify" ]; then
    log "Verifying the turing-skills fork (read-only)"
    [ -d "${DEST_REPO}/.git" ] || die "No fork at ${DEST_REPO}. Run with --apply first."

    failures=0

    step "No remotes — the agent cannot push into the operator's repos"
    remotes="$(git -C "$DEST_REPO" remote)"
    if [ -z "$remotes" ]; then
        ok "no remotes configured"
    else
        warn "remotes present: ${remotes}"
        failures=$((failures + 1))
    fi

    step "The measurement layer is absent"
    for path in $MEASUREMENT_PATHS; do
        if [ -e "${DEST_REPO}/${path}" ]; then
            warn "PRESENT in the fork (the agent can edit its own ruler): ${path}"
            failures=$((failures + 1))
        else
            ok "absent: ${path}"
        fi
    done

    step "Provenance is recorded"
    if [ -f "${DEST_REPO}/${PROVENANCE_FILE}" ]; then
        ok "${PROVENANCE_FILE} present"
        sed -n '1,20p' "${DEST_REPO}/${PROVENANCE_FILE}" | sed 's/^/    /'
    else
        warn "missing: ${DEST_REPO}/${PROVENANCE_FILE} — the fork's source commit is unrecorded"
        failures=$((failures + 1))
    fi

    step "The fork point is tagged"
    if git -C "$DEST_REPO" rev-parse -q --verify "refs/tags/${FORK_TAG}" >/dev/null; then
        ok "tag ${FORK_TAG} → $(git -C "$DEST_REPO" rev-parse "${FORK_TAG}")"
    else
        warn "no ${FORK_TAG} tag — rollback has no unambiguous floor"
        failures=$((failures + 1))
    fi

    echo
    if [ "$failures" -eq 0 ]; then
        ok "Fork is sound."
        exit 0
    fi
    die "${failures} check(s) failed."
fi

[ -d "${SOURCE_REPO}/.git" ] || die "Source is not a git repo: ${SOURCE_REPO}"

SOURCE_HEAD="$(git -C "$SOURCE_REPO" rev-parse HEAD)"
SOURCE_BRANCH="$(git -C "$SOURCE_REPO" rev-parse --abbrev-ref HEAD)"
SOURCE_DIRTY="clean"
[ -n "$(git -C "$SOURCE_REPO" status --porcelain)" ] && SOURCE_DIRTY="DIRTY"
[ -z "$FREEZE_COMMIT" ] && FREEZE_COMMIT="$SOURCE_HEAD"

# Resolve whatever was passed to a full sha so the provenance file is exact.
FREEZE_COMMIT="$(git -C "$SOURCE_REPO" rev-parse --verify "${FREEZE_COMMIT}^{commit}" 2>/dev/null)" \
    || die "Not a commit in ${SOURCE_REPO}: ${FREEZE_COMMIT}"
FREEZE_SUBJECT="$(git -C "$SOURCE_REPO" log -1 --format='%s' "$FREEZE_COMMIT")"
FREEZE_DATE="$(git -C "$SOURCE_REPO" log -1 --format='%cI' "$FREEZE_COMMIT")"
TODAY="$(date -u '+%Y-%m-%d')"

log "turing-skills fork — ${MODE} mode"
cat <<EOF
  source repo    : ${SOURCE_REPO}
  source branch  : ${SOURCE_BRANCH} (working tree: ${SOURCE_DIRTY})
  frozen at      : ${FREEZE_COMMIT}
                   ${FREEZE_DATE}  ${FREEZE_SUBJECT}
  destination    : ${DEST_REPO}
  removed in fork: ${MEASUREMENT_PATHS}
EOF

if [ "$SOURCE_DIRTY" = "DIRTY" ]; then
    warn "The source working tree has uncommitted changes. They are NOT carried into"
    warn "the fork — the fork is taken from a commit, not from the working tree. If"
    warn "those changes belong in the scaffold baseline, commit them in the source"
    warn "repo first and re-run."
fi

if [ -e "$DEST_REPO" ]; then
    warn "Destination already exists: ${DEST_REPO}"
    warn "Refusing to recreate it. A fork re-taken from a later source commit is a"
    warn "different scaffold baseline, and swapping it under a running trajectory"
    warn "silently invalidates every round already measured. Move it aside by hand"
    warn "if you genuinely mean to start over."
    [ "$MODE" = "apply" ] && die "Nothing was changed."
fi

if [ "$MODE" = "plan" ]; then
    cat <<'EOF'

  PLAN ONLY. Nothing below has been executed. Read it, then re-run with
  --apply, and --verify afterwards.
EOF
fi

if [ "$MODE" = "apply" ] && [ "$ASSUME_YES" -eq 0 ]; then
    echo
    warn "This creates a new local git repository at ${DEST_REPO}."
    read -r -p "Type 'apply' to proceed: " CONFIRM || CONFIRM=""
    [ "$CONFIRM" = "apply" ] || die "Not confirmed — nothing was changed."
fi

step "1. Clone the source repo, without hardlinking its object store"
info "--no-hardlinks costs disk and buys independence: the fork's objects are its"
info "own, so nothing the agent does can touch the source repo's storage."
run_step "git clone --no-hardlinks '${SOURCE_REPO}' '${DEST_REPO}'"

step "2. Freeze at ${FREEZE_COMMIT}"
info "'main' in the fork starts at the frozen commit. Everything the source repo"
info "gained after it is discarded, so the scaffold baseline is a single named"
info "commit rather than 'whatever the skills repo happened to be that day'."
run_step "git -C '${DEST_REPO}' checkout -B main ${FREEZE_COMMIT}"
run_step "git -C '${DEST_REPO}' tag -f ${FORK_TAG} ${FREEZE_COMMIT}"

step "3. Remove the inherited remote"
info "Nothing is created remotely, and nothing can be pushed anywhere."
run_step "git -C '${DEST_REPO}' remote remove origin"

step "4. Remove the measurement layer"
info "The ruler does not live in the repo being measured. Canonical copy stays at"
info "${SOURCE_REPO}/${MEASUREMENT_PATHS}."
for path in $MEASUREMENT_PATHS; do
    run_step "git -C '${DEST_REPO}' rm -r --quiet '${path}'"
done

step "5. Record provenance"
info "Written into the fork so the source commit survives being copied to another"
info "machine, and so a round record can cite it."
if [ "$MODE" = "apply" ]; then
    cat > "${DEST_REPO}/${PROVENANCE_FILE}" <<EOF
# turing-skills — fork provenance

This repository is the **scaffold** for Turing's autonomous research agent. Its
git history *is* the scaffold version history: one SHA per round, \`git revert\`
is rollback, and a diff answers "what changed between round N and N+1".

| Field | Value |
|---|---|
| Forked from | \`${SOURCE_REPO}\` |
| Source branch at fork time | \`${SOURCE_BRANCH}\` |
| **Frozen at commit** | \`${FREEZE_COMMIT}\` |
| Commit date | ${FREEZE_DATE} |
| Commit subject | ${FREEZE_SUBJECT} |
| Source tree at fork time | ${SOURCE_DIRTY} |
| Fork created | ${TODAY} |
| Fork point tag | \`${FORK_TAG}\` |

Created by \`scripts/bootstrap-turing-skills.sh\` in the Turing repo.

## Removed from this fork

\`${MEASUREMENT_PATHS}\` — the measurement layer (\`SKILL.md\`,
\`conventions.md\`, \`driving-functions.md\`, \`log_run.py\`). The agent commits
freely to this repository, so anything left in it is editable by the agent, and
an agent that can edit its own ruler is not measured. The canonical copy lives
in the source repo, outside the agent's write surface.

## Rules for this repository

- The self-editing agent is **exempt from the PR/review process here, and only
  here**. It commits freely. Do not expect review to have happened.
- The Turing repo keeps its full process — issue-assignee claiming, one branch =
  one agent, PR with green CI, CODEOWNERS hotspots, human review tiers.
- Self-edits reach skills, **never Turing's source**.
- No remote is configured, by design. Adding one is an operator decision.
- Divergence from \`${SOURCE_REPO}\` is expected and accepted. No merge-back is
  planned; the operator's own \`skillify\` edits must not land here, because
  they would show up in the round delta as scaffold improvement.

## Provenance rule

Do not recreate this fork from a later source commit while a trajectory is
running. A different fork point is a different scaffold baseline, and swapping it
mid-trajectory invalidates every round already measured.

Source of record: \`Turing/research/briefs/2026-08-12-autonomous-research-agent.md\`
Decision record: \`Turing/docs/adr/0011-autonomous-research-agent-retarget.md\` § 6
EOF
    printf '    $ %s\n' "cat > ${DEST_REPO}/${PROVENANCE_FILE} <<'EOF' ... EOF"
else
    printf '    $ %s\n' "cat > ${DEST_REPO}/${PROVENANCE_FILE} <<'EOF' ... EOF"
    info "(records source path, branch, frozen SHA ${FREEZE_COMMIT}, commit date,"
    info " what was removed and why, and the no-remote / no-merge-back rules)"
fi

step "6. Commit the fork point"
run_step "git -C '${DEST_REPO}' add '${PROVENANCE_FILE}'"
run_step "git -C '${DEST_REPO}' commit -m 'Fork turing-skills from ${FREEZE_COMMIT} (measurement layer removed)'"

step "7. Verify"
printf '    $ %s\n' "bash scripts/bootstrap-turing-skills.sh --verify --dest '${DEST_REPO}'"

echo
if [ "$MODE" = "plan" ]; then
    log "Plan complete — nothing was changed. Re-run with --apply to carry it out."
else
    log "Fork created at ${DEST_REPO}, frozen at ${FREEZE_COMMIT}."
    warn "Record that SHA in the round-0 run's engine identity (scaffold_git_sha)."
fi
