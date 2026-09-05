#!/usr/bin/env bash
# Frozen verifier for the RSI problem "import-speedup".
# Gate 1: the research-loop test suite passes against THIS sandbox's src/.
# Gate 2: plots still render (test_plots is inside the gate).
# Score: median import wall-time of turing.research.loop.run in a pristine
# reference checkout divided by the same in this sandbox, interleaved so
# machine load hits both equally. Higher is better; 1.0 = no change.
set -euo pipefail
SANDBOX="$(pwd)"
REF=/home/user/rsi-reference
PY=/home/user/Turing/.venv/bin/python
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export PYTHONDONTWRITEBYTECODE=1
PYTHONPATH="$SANDBOX/src" "$PY" -m pytest "$SANDBOX/tests/test_research/test_loop" -q -o addopts="" -x -p no:cacheprovider >/tmp/rsi-verify-pytest.log 2>&1 || { tail -20 /tmp/rsi-verify-pytest.log; echo "verifier: research-loop tests failed"; exit 1; }
tail -1 /tmp/rsi-verify-pytest.log
measure() { PYTHONPATH="$1/src" "$PY" -c 'import time,sys;t=time.perf_counter();import turing.research.loop.run;print(time.perf_counter()-t)'; }
ref=(); box=()
for i in 1 2 3 4 5 6 7; do ref+=("$(measure "$REF")"); box+=("$(measure "$SANDBOX")"); done
"$PY" - "${ref[@]}" -- "${box[@]}" <<'PYEOF'
import statistics, sys
args = sys.argv[1:]
i = args.index("--")
ref = [float(x) for x in args[:i]]
box = [float(x) for x in args[i+1:]]
r, b = statistics.median(ref), statistics.median(box)
print(f"reference_median_s={r:.4f} sandbox_median_s={b:.4f}")
print(f"score={r/b:.4f}")
PYEOF
