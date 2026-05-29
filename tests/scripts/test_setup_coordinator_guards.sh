#!/usr/bin/env bash
# Regression test for the load-bearing guard/ordering logic in
# scripts/setup-coordinator.sh (ADR 0010 §5, Slice G2). The full provisioning
# script can only run inside WSL2 Ubuntu, so this exercises the two kernels that
# CAN run off-WSL2 in isolation:
#
#   1. require_wsl2 — the host refusal. It must reject a missing osrelease and a
#      non-WSL osrelease (a Jetson / bare Linux / mac), and accept a WSL/Microsoft
#      one. This is the gate that keeps the systemd + Tailscale-in-WSL2
#      assumptions from running on the wrong host (ADR 0010 §5/§8).
#
#   2. Phase 5 seed-durability ordering — the sentinel coordinator.seed that arms
#      the re-run guard MUST be committed (`mv`) only AFTER the worker seeds are
#      persisted, the conf is rendered, and the coordinator seed/URL are written
#      to .env. A mid-section failure that left the sentinel armed with the worker
#      seeds lost is the bug this asserts against (recon #3).
#
# Rather than mirror logic, we extract the REAL require_wsl2 function from the
# shipped script and run it verbatim (rewriting only the hardcoded osrelease path
# to a test file, the same surgical-substitution the jetson NATS harness uses on
# ENV_PATH). The ordering check is a static assertion over the shipped source.
#
# Run: bash tests/scripts/test_setup_coordinator_guards.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../../scripts/setup-coordinator.sh"

[[ -r "$SCRIPT" ]] || { echo "FAIL - cannot read $SCRIPT"; exit 1; }

fail=0
check() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        printf 'ok   - %s\n' "$name"
    else
        printf 'FAIL - %s (expected %q, got %q)\n' "$name" "$expected" "$actual"
        fail=1
    fi
}

# ── 1. require_wsl2 refusal ───────────────────────────────────────────
# Pull the real function body out of the shipped script (between the `require_wsl2() {`
# header and its closing brace at column 0). If the anchors drift, fail loudly so
# the test is updated alongside the script.
WSL2_FN="$(
    awk '
        /^require_wsl2\(\) \{$/ { f = 1 }
        f { print }
        f && /^\}$/ { exit }
    ' "$SCRIPT"
)"
if [[ -z "$WSL2_FN" ]] \
    || ! grep -q 'osrelease' <<<"$WSL2_FN" \
    || ! grep -q 'die ' <<<"$WSL2_FN"; then
    echo "FAIL - could not extract require_wsl2 from setup-coordinator.sh"
    echo "       (anchor lines likely changed — update this test to match)"
    exit 1
fi

# The function hardcodes /proc/sys/kernel/osrelease (absent on a mac/CI runner),
# so rewrite that literal to a test-controlled $OSRELEASE_PATH. Everything else —
# the [[ -r ]] guard, the lowercasing, the *microsoft*/*wsl* match, the die calls
# — runs verbatim, so any edit to the branch logic is exercised here.
WSL2_FN_TESTABLE="${WSL2_FN//\/proc\/sys\/kernel\/osrelease/\$OSRELEASE_PATH}"

run_require_wsl2() {
    local osrelease_path="$1"
    # fd 3 carries the result out; the function's own stdout (the "detected WSL2"
    # success notice) is silenced so only the sentinel surfaces. `die` reports
    # through fd 3 (not the silenced fd 1) so the refusal path stays observable.
    bash -c '
        set -euo pipefail
        exec 3>&1
        die() { echo "__DIE__" >&3; exit 0; }
        OSRELEASE_PATH="'"$osrelease_path"'"
        '"$WSL2_FN_TESTABLE"'
        { require_wsl2 && echo "__OK__" >&3; } >/dev/null
    ' || echo "__ABORT__"
}

MISSING="/nonexistent/proc/osrelease"
WSL_FILE="$(mktemp)"; printf '5.15.167.4-microsoft-standard-WSL2\n' >"$WSL_FILE"
WSL_LC="$(mktemp)"; printf '6.6.0-wsl2-something\n' >"$WSL_LC"
BARE_FILE="$(mktemp)"; printf '5.15.0-91-generic\n' >"$BARE_FILE"  # bare Ubuntu/Jetson
trap 'rm -f "$WSL_FILE" "$WSL_LC" "$BARE_FILE"' EXIT

check "require_wsl2: missing osrelease is refused" \
    "__DIE__" "$(run_require_wsl2 "$MISSING")"

check "require_wsl2: bare-Linux osrelease (no microsoft/wsl) is refused" \
    "__DIE__" "$(run_require_wsl2 "$BARE_FILE")"

check "require_wsl2: '...-microsoft-...-WSL2' osrelease is accepted" \
    "__OK__" "$(run_require_wsl2 "$WSL_FILE")"

check "require_wsl2: lowercase 'wsl2' osrelease is accepted" \
    "__OK__" "$(run_require_wsl2 "$WSL_LC")"

# ── 2. Phase 5 seed-durability ordering ───────────────────────────────
# Recon #3: the sentinel coordinator.seed (which arms the re-run skip guard) must
# be committed ONLY after the worker seeds are durably persisted, the conf is
# rendered, and the coordinator seed/URL are in .env. We assert this as a static
# ordering invariant over the shipped source: the line that promotes the temp
# seed to its final path (`mv "$COORD_SEED_TMP" "$COORD_SEED_FILE"`) must come
# AFTER (a) the worker-seed write loop, (b) the nats-server.conf install, and
# (c) the coordinator .env seed write — so an interrupted run can never strand
# the worker seeds with the guard already tripped.
line_of() { grep -n -m1 -- "$1" "$SCRIPT" | cut -d: -f1; }

# The patterns below are literal grep needles matched against the source — the
# `$VAR` text inside them is the script's own variable names, not expansions
# here. SC2016 is therefore expected, not a bug.
# shellcheck disable=SC2016
WORKER_SEED_WRITE="$(line_of "nk -gen user > '\$wseed_file'")"
# shellcheck disable=SC2016
CONF_INSTALL="$(line_of 'sudo install -m 600 "$NATS_CONF_TMP" "$NATS_CONF"')"
COORD_ENV_WRITE="$(line_of "TURING_NATS_NKEY_SEED=%s")"
# shellcheck disable=SC2016
SENTINEL_COMMIT="$(line_of 'sudo mv "$COORD_SEED_TMP" "$COORD_SEED_FILE"')"

if [[ -z "$WORKER_SEED_WRITE" || -z "$CONF_INSTALL" || -z "$COORD_ENV_WRITE" || -z "$SENTINEL_COMMIT" ]]; then
    echo "FAIL - could not locate one of the Phase 5 ordering anchors"
    echo "       worker_seed=$WORKER_SEED_WRITE conf=$CONF_INSTALL env=$COORD_ENV_WRITE sentinel=$SENTINEL_COMMIT"
    fail=1
else
    check "ordering: sentinel committed AFTER worker seeds persisted" \
        "yes" "$([[ "$SENTINEL_COMMIT" -gt "$WORKER_SEED_WRITE" ]] && echo yes || echo no)"
    check "ordering: sentinel committed AFTER nats-server.conf installed" \
        "yes" "$([[ "$SENTINEL_COMMIT" -gt "$CONF_INSTALL" ]] && echo yes || echo no)"
    check "ordering: sentinel committed AFTER coordinator seed written to .env" \
        "yes" "$([[ "$SENTINEL_COMMIT" -gt "$COORD_ENV_WRITE" ]] && echo yes || echo no)"
fi

# The generation block must write the coordinator seed to a TEMP path first
# (NOT the sentinel path), or the guard would trip before the durable writes.
# shellcheck disable=SC2016
check "ordering: coordinator seed generated to a temp path first" \
    "yes" "$(grep -q "nk -gen user > '\$COORD_SEED_TMP'" "$SCRIPT" && echo yes || echo no)"

if [[ "$fail" -ne 0 ]]; then
    echo "RESULT: FAIL"
    exit 1
fi
echo "RESULT: PASS"
