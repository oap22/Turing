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
#
# Two engines behind one flag set. With only the original flags, on a slug
# that has never been locked, this script runs the bash loop below,
# unchanged. It execs the Python engine, `python -m turing.research.rsi`,
# forwarding every flag, when any of these holds:
#   * `--verifier <cmd>` is given (a first run of the Python engine);
#   * `TURING_RSI_ENGINE=python` is set;
#   * the slug's sandbox already holds VERIFIER.json — the marker that this
#     slug is a verified loop. The desktop's argv (no --verifier) therefore
#     resumes a locked slug on the Python engine instead of silently
#     downgrading to the unverified bash loop, whose `wc -l` numbering would
#     also skip over the engine's event lines.
# Same sandbox and results layout, plus a frozen verifier that measures each
# round, a frozen failure taxonomy, a scaffold self-edit step with rollback,
# and a cheat detector. See docs/rsi-loop.md. `--verifier-file`,
# `--self-edit-every`, `--self-edit-budget` and `--noise-floor` belong to
# that engine and are refused without it. The Python engine needs
# `--verifier` on a first run: `TURING_RSI_ENGINE=python` alone on a fresh
# slug exits 2.
#
# Interpreter: `.venv/bin/python` under the repo when present, else
# `TURING_RSI_PYTHON` if set, else `python3` with the repo's `src/` on
# PYTHONPATH (src layout; a bare `python3` cannot import `turing` otherwise).

set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: rsi-loop.sh --slug <slug> --results-root <dir> [--problem <text>] [--rounds <n>] [--dry-run]

  --slug <slug>          required; must match ^[a-z0-9-]+$
  --results-root <dir>   required; results stream here under loop-rsi-<slug>/
  --problem <text>       required on first run (ignored on resume; PROBLEM.md wins)
  --rounds <n>           default 10; 0 means unlimited
  --dry-run              print the resolved plan and exit without touching disk

Python engine (python -m turing.research.rsi; selected by --verifier, by TURING_RSI_ENGINE=python,
or automatically when ~/turing-workspace/rsi-<slug>/VERIFIER.json already exists):
  --verifier <cmd>       frozen verifier command, run from the sandbox after every round;
                         required on the first run, locked into VERIFIER.json
  --verifier-file <rel>  sandbox file whose sha256 joins the lock; repeatable. Only the
                         command's FIRST token is pinned automatically, so
                         --verifier 'python grade.py' needs --verifier-file grade.py
  --self-edit-every <n>  let the agent rewrite SCAFFOLD.md every n rounds (default 3; 0 disables)
  --self-edit-budget <n> max self-edits per invocation (default 3)
  --noise-floor <x>      rollback noise floor override (default: stdev of prior scores)
EOF
}

SLUG=""
RESULTS_ROOT=""
PROBLEM=""
ROUNDS=10
DRY_RUN=0
VERIFIER=""
PY_ONLY_ARGS=()

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
    --verifier)
      VERIFIER="${2:-}"
      shift 2
      ;;
    --verifier-file|--self-edit-every|--self-edit-budget|--noise-floor)
      PY_ONLY_ARGS+=("$1" "${2:-}")
      shift 2
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
# pass a literal `~` or a relative path; bash does not tilde-expand a value
# that arrived through a variable, so expand it ourselves, and anchor a
# relative path to the invocation cwd *before* either engine sees it — the
# Python engine is exec'd from wherever we are, and a results dir must not
# move with the process's cwd.
if [[ "$RESULTS_ROOT" == "~"* ]]; then
  RESULTS_ROOT="${HOME}${RESULTS_ROOT:1}"
fi
if [[ "$RESULTS_ROOT" != /* ]]; then
  RESULTS_ROOT="$PWD/$RESULTS_ROOT"
fi

SANDBOX="$HOME/turing-workspace/rsi-$SLUG"

# Bridge to the Python engine. Everything below this block is the original
# bash loop and runs only when nothing selected the Python engine: no
# --verifier, no TURING_RSI_ENGINE=python, and no VERIFIER.json in the
# sandbox. The desktop's argv contract sees no change on an unlocked slug.
if [[ -n "$VERIFIER" || "${TURING_RSI_ENGINE:-}" == "python" || -f "$SANDBOX/VERIFIER.json" ]]; then
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
  elif [[ -n "${TURING_RSI_PYTHON:-}" ]]; then
    PYTHON="$TURING_RSI_PYTHON"
  else
    PYTHON="python3"
    export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
  fi
  PY_ARGS=(--slug "$SLUG" --results-root "$RESULTS_ROOT" --rounds "$ROUNDS")
  [[ -n "$PROBLEM" ]] && PY_ARGS+=(--problem "$PROBLEM")
  [[ -n "$VERIFIER" ]] && PY_ARGS+=(--verifier "$VERIFIER")
  [[ "$DRY_RUN" -eq 1 ]] && PY_ARGS+=(--dry-run)
  if [[ ${#PY_ONLY_ARGS[@]} -gt 0 ]]; then
    PY_ARGS+=("${PY_ONLY_ARGS[@]}")
  fi
  # No `cd`: the interpreter above can import `turing` from anywhere, and
  # staying put keeps the process cwd honest for anything else relative.
  exec "$PYTHON" -m turing.research.rsi "${PY_ARGS[@]}"
fi
if [[ ${#PY_ONLY_ARGS[@]} -gt 0 ]]; then
  echo "error: ${PY_ONLY_ARGS[0]} requires the Python engine (pass --verifier or set TURING_RSI_ENGINE=python)" >&2
  usage
  exit 2
fi

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
