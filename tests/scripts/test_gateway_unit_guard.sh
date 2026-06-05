#!/usr/bin/env bash
# Regression test for the turing-gateway.service ExecStart guard (ADR-0010 §6,
# #300). The guard's whole job is to decide — from TURING_GATEWAY_STANDALONE +
# TURING_GATEWAY_ENABLED — whether to START the dedicated gateway or HOLD as a
# no-op, and it MUST refuse to start a second gateway while the coordinator's
# in-process one still owns gateway_port (else it crash-loops on the bind).
#
# We extract the REAL inline `/bin/sh -c '...'` body from the shipped unit and
# run it with the python/sleep execs stubbed, so the actual shipped decision
# logic is exercised (any edit to the guard is caught here).
#
# Run: bash tests/scripts/test_gateway_unit_guard.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="$HERE/../../scripts/coordinator/turing-gateway.service"
[[ -r "$UNIT" ]] || { echo "FAIL - cannot read $UNIT"; exit 1; }

# Pull the body out of `ExecStart=/bin/sh -c '<body>'`. The body contains no
# single quotes (systemd inline avoids them), so strip the wrapping quotes.
BODY="$(sed -n "s/^ExecStart=\/bin\/sh -c '\(.*\)'$/\1/p" "$UNIT")"
if [[ -z "$BODY" ]]; then
    echo "FAIL - could not extract the ExecStart body (unit format changed — update this test)"
    exit 1
fi

# Stubs: a fake python (absolute path in the body is rewritten to this) and a
# fake `sleep` on PATH. The python stub answers the `-c` import probe with 0
# (module present) and prints __START__ for `-m turing.gateway`; `exec sleep`
# prints __HOLD__. Both replace the shell via `exec`, so each run prints exactly
# one sentinel.
STUBDIR="$(mktemp -d)"
trap 'rm -rf "$STUBDIR"' EXIT
cat >"$STUBDIR/python" <<'PY'
#!/bin/sh
for a in "$@"; do
    [ "$a" = "turing.gateway" ] && { echo __START__; exit 0; }
done
exit 0   # the import probe → module present
PY
cat >"$STUBDIR/sleep" <<'SL'
#!/bin/sh
echo __HOLD__
exit 0
SL
chmod +x "$STUBDIR/python" "$STUBDIR/sleep"

# Rewrite the hardcoded venv python path to the stub (| delimiter avoids /).
BODY_T="$(printf '%s' "$BODY" | sed "s|/home/turing/venv/bin/python|$STUBDIR/python|g")"

fail=0
# Run the guard body with the given env, return the sentinel it prints.
decide() {  # $1=STANDALONE $2=ENABLED
    PATH="$STUBDIR:$PATH" TURING_GATEWAY_STANDALONE="$1" TURING_GATEWAY_ENABLED="$2" \
        sh -c "$BODY_T" 2>/dev/null | grep -oE '__START__|__HOLD__' | head -1
}
check() {  # $1=name $2=expected $3=actual
    if [[ "$2" == "$3" ]]; then printf 'ok   - %s\n' "$1"
    else printf 'FAIL - %s (expected %q, got %q)\n' "$1" "$2" "$3"; fail=1; fi
}

# Opt-out: standalone unset → hold (in-process gateway is the default).
check "standalone unset → hold"                 "__HOLD__"  "$(decide "" "true")"
check "standalone=0 → hold"                     "__HOLD__"  "$(decide "0" "false")"
check "standalone=garbage → hold"               "__HOLD__"  "$(decide "maybe" "false")"

# The crash-loop guard (the review's MEDIUM): standalone on, but ENABLED still
# truthy / unset → MUST hold rather than double-bind the coordinator's port.
check "standalone=1 + enabled=true → hold (no double-bind)"  "__HOLD__"  "$(decide "1" "true")"
check "standalone=1 + enabled unset → hold (fail-safe)"      "__HOLD__"  "$(decide "1" "")"
check "standalone=1 + enabled=1 → hold"                      "__HOLD__"  "$(decide "1" "1")"

# Correct opt-in: standalone on AND enabled explicitly false → start.
check "standalone=1 + enabled=false → start"    "__START__" "$(decide "1" "false")"
check "standalone=1 + enabled=0 → start"        "__START__" "$(decide "1" "0")"
check "standalone=1 + enabled=off → start"      "__START__" "$(decide "1" "off")"

# Case-insensitive truthy/falsey matching.
check "standalone=True + enabled=FALSE → start" "__START__" "$(decide "True" "FALSE")"
check "standalone=ON + enabled=No → start"      "__START__" "$(decide "ON" "No")"

if [[ "$fail" -ne 0 ]]; then echo "RESULT: FAIL"; exit 1; fi
echo "RESULT: PASS"
