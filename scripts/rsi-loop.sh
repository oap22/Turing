#!/usr/bin/env bash
# The RSI workstation loop engine: a continuous Claude Code loop that
# iterates on a problem statement round after round inside a dedicated
# sandbox directory, streaming metrics.jsonl / trajectory.json / plots into
# a results directory the desktop's panes watch live.
#
# Invoked by the desktop's loop terminal — see `rsiRunner()` in
# `webui/src/desktop/rsi.ts`, which builds the exact argv below and hands it
# to TermPane to pre-type (never auto-run) into a fresh terminal pane. Also
# runnable by hand for debugging or for running an experiment outside the
# desktop app.
#
# Sandbox convention: isolation here is a dedicated directory
# (~/turing-workspace/rsi-<slug>) plus the round prompt's own instructions,
# not an OS-level boundary. `--permission-mode bypassPermissions` is scoped
# to that directory only by convention, not by an OS user or container — see
# `scripts/setup-research-sandbox.sh` and ADR 0011 §8 for the stronger
# boundary (a dedicated `turing` OS user) this script deliberately does not
# implement.

set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: rsi-loop.sh --slug <slug> --results-root <dir> [--problem <text>] [--rounds <n>] [--dry-run]

  --slug <slug>          required; must match ^[a-z0-9-]+$
  --results-root <dir>   required; results stream here under loop-rsi-<slug>/
  --problem <text>       required on first run (ignored on resume; PROBLEM.md wins)
  --rounds <n>           default 10; 0 means unlimited
  --dry-run              print the resolved plan and exit without touching disk
EOF
}

SLUG=""
RESULTS_ROOT=""
PROBLEM=""
ROUNDS=10
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)
      SLUG="${2:-}"
      shift 2
      ;;
    --results-root)
      RESULTS_ROOT="${2:-}"
      shift 2
      ;;
    --problem)
      PROBLEM="${2:-}"
      shift 2
      ;;
    --rounds)
      ROUNDS="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      echo "error: unknown flag: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$SLUG" ]]; then
  echo "error: --slug is required" >&2
  usage
  exit 2
fi
if [[ ! "$SLUG" =~ ^[a-z0-9-]+$ ]]; then
  echo "error: --slug must match ^[a-z0-9-]+\$ (got: $SLUG)" >&2
  usage
  exit 2
fi
if [[ -z "$RESULTS_ROOT" ]]; then
  echo "error: --results-root is required" >&2
  usage
  exit 2
fi
if [[ ! "$ROUNDS" =~ ^[0-9]+$ ]]; then
  echo "error: --rounds must be a non-negative integer (got: $ROUNDS)" >&2
  usage
  exit 2
fi

# The desktop passes an already-expanded absolute path, but hand runs may
# pass a literal `~`; bash does not tilde-expand a value that arrived through
# a variable, so expand it ourselves.
if [[ "$RESULTS_ROOT" == "~"* ]]; then
  RESULTS_ROOT="${HOME}${RESULTS_ROOT:1}"
fi

SANDBOX="$HOME/turing-workspace/rsi-$SLUG"
RESULTS="$RESULTS_ROOT/loop-rsi-$SLUG"

if [[ "$DRY_RUN" -eq 1 ]]; then
  RESUMING="no"
  [[ -f "$SANDBOX/PROBLEM.md" ]] && RESUMING="yes"
  echo "sandbox: $SANDBOX"
  echo "results: $RESULTS"
  echo "rounds: $ROUNDS"
  echo "resuming: $RESUMING"
  exit 0
fi

mkdir -p "$SANDBOX" "$RESULTS"
cd "$SANDBOX"

if [[ ! -d .git ]]; then
  git init -q
fi

if [[ ! -f PROBLEM.md ]]; then
  if [[ -z "$PROBLEM" ]]; then
    echo "error: --problem is required on the first run (no PROBLEM.md in $SANDBOX)" >&2
    usage
    exit 2
  fi
  printf '%s\n' "$PROBLEM" > PROBLEM.md
else
  # PROBLEM.md already exists from an earlier run — it wins. A --problem
  # passed alongside a resume is silently ignored rather than overwriting the
  # sandbox's actual goal out from under it.
  :
fi
touch NOTES.md

# Rounds keep incrementing across a resumed loop rather than restarting at 1:
# the starting round is however many trajectory lines already exist, plus one.
START=1
if [[ -f "$RESULTS/trajectory.json" ]]; then
  EXISTING=$(wc -l < "$RESULTS/trajectory.json" | tr -d ' ')
  START=$((EXISTING + 1))
fi

consecutive_failures=0
rounds_run=0
round="$START"

while true; do
  if [[ "$ROUNDS" -ne 0 && "$round" -gt $((START + ROUNDS - 1)) ]]; then
    break
  fi

  if [[ -e "$SANDBOX/STOP" ]]; then
    echo "stopping: found STOP file in $SANDBOX"
    break
  fi

  echo "=== rsi round $round — $SLUG ==="

  PROMPT=$(cat <<EOF
You are round $round of a continuous research loop. Your sandbox is this
working directory; you may write only here and in $RESULTS. Read
PROBLEM.md (the goal) and NOTES.md (state from earlier rounds). Do ONE
focused iteration of research, building, or experimentation toward the
problem. Before you finish: (1) append exactly one JSON line to
$RESULTS/metrics.jsonl of the form {"step": $round, "ts": <epoch seconds>, ...}
including every numeric measurement you produced this round; (2) save any
plots or figures as .png or .svg files in $RESULTS; (3) update NOTES.md with
what you found and what the next round should try; (4) commit your work with
git. If the problem is solved, or you are convinced no further progress is
possible, create an empty file named STOP in the working directory and
record why in NOTES.md.
EOF
)

  started=$(date +%s)
  exit_code=0
  claude -p "$PROMPT" --permission-mode bypassPermissions || exit_code=$?
  ended=$(date +%s)

  printf '{"round": %s, "started": %s, "ended": %s, "exit": %s}\n' \
    "$round" "$started" "$ended" "$exit_code" >> "$RESULTS/trajectory.json"

  if [[ "$exit_code" -ne 0 ]]; then
    consecutive_failures=$((consecutive_failures + 1))
    echo "round $round: claude exited $exit_code (consecutive failures: $consecutive_failures)" >&2
    if [[ "$consecutive_failures" -ge 3 ]]; then
      echo "error: 3 consecutive claude failures — aborting. Is the claude CLI installed?" >&2
      break
    fi
  else
    consecutive_failures=0
  fi

  rounds_run=$((rounds_run + 1))
  round=$((round + 1))
done

echo "done: $rounds_run round(s) completed this invocation — results in $RESULTS"
