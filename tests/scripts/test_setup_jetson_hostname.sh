#!/usr/bin/env bash
# Regression test for the idempotent hostname prompt in scripts/setup-jetson.sh
# (ADR 0010, Slice J — "second run is a clean no-op").
#
# The full setup script can only run on a real Jetson, so this exercises the
# load-bearing kernel in isolation: the prompt must NOT block or abort an
# unattended/already-provisioned re-run, and must require a hostname only when
# there is genuinely nothing to fall back to.
#
# Run: bash tests/scripts/test_setup_jetson_hostname.sh

set -uo pipefail

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

# Mirror of the resolve logic in setup-jetson.sh. `hostnamectl` is stubbed so
# the test is host-independent. `read || true` + default expansion is exactly
# what the script does.
resolve_hostname() {
    local current_static="$1"        # what `hostnamectl --static` would print
    local typed="$2"                 # what the operator types ("" == Enter)
    local closed_stdin="${3:-no}"    # "yes" == EOF (unattended / piped re-run)

    local HOSTNAME_CURRENT HOSTNAME_NEW
    HOSTNAME_CURRENT="$current_static"
    if [[ "$closed_stdin" == "yes" ]]; then
        read -r HOSTNAME_NEW </dev/null || true
    else
        HOSTNAME_NEW="$typed"
    fi
    HOSTNAME_NEW="${HOSTNAME_NEW:-$HOSTNAME_CURRENT}"
    if [[ -z "$HOSTNAME_NEW" ]]; then
        echo "__DIE__"
        return 0
    fi
    echo "$HOSTNAME_NEW"
}

# First run: operator types a hostname → that value wins.
check "first run: typed value is used" \
    "jetson-1" "$(resolve_hostname "ubuntu" "jetson-1")"

# Re-run, operator presses Enter → keeps the already-set hostname (no-op).
check "re-run: empty input keeps current hostname" \
    "jetson-1" "$(resolve_hostname "jetson-1" "")"

# Re-run, fully unattended (stdin closed / EOF) → keeps current, does NOT hang
# or abort under set -e. This is the Slice J "clean no-op" requirement.
check "re-run: closed stdin keeps current hostname (no hang/abort)" \
    "jetson-1" "$(resolve_hostname "jetson-1" "" "yes")"

# First run on an unconfigured box where the operator just hits Enter and there
# is no usable current hostname → must still demand a value (die path).
check "first run: empty input + no current hostname dies" \
    "__DIE__" "$(resolve_hostname "" "")"

# Operator explicitly renames an already-provisioned host → typed value wins.
check "re-run: explicit rename overrides current" \
    "jetson-2" "$(resolve_hostname "jetson-1" "jetson-2")"

if [[ "$fail" -ne 0 ]]; then
    echo "RESULT: FAIL"
    exit 1
fi
echo "RESULT: PASS"
