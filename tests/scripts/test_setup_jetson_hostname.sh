#!/usr/bin/env bash
# Regression test for the idempotent hostname prompt in scripts/setup-jetson.sh
# (ADR 0010, Slice J — "second run is a clean no-op").
#
# The full setup script can only run on a real Jetson, so this exercises the
# load-bearing kernel in isolation: the prompt must NOT block or abort an
# unattended/already-provisioned re-run, and must require a hostname only when
# there is genuinely nothing to fall back to.
#
# Rather than mirror the logic, we extract the REAL resolve block from the
# shipped script and run it under the same `set -euo pipefail`, with
# `hostnamectl`, `die`, and `read`'s stdin stubbed. So:
#   (a) any edit to the prompt block in setup-jetson.sh is exercised here, and
#   (b) the set -e/EOF interaction is real — dropping the `|| true` after
#       `read` would let a closed-stdin re-run abort under set -e, which this
#       test would catch as a FAIL (see the closed-stdin case).
#
# Run: bash tests/scripts/test_setup_jetson_hostname.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../../scripts/setup-jetson.sh"

[[ -r "$SCRIPT" ]] || { echo "FAIL - cannot read $SCRIPT"; exit 1; }

# Pull the real resolve block out of the shipped script, between two stable,
# unique anchor lines (inclusive of the leading hostname read, up to but not
# including NODE_NAME=). If the block can't be found the anchors drifted —
# fail loudly so the test is updated alongside the script.
RESOLVE_BLOCK="$(
    awk '
        /^HOSTNAME_CURRENT="\$\{TURING_HOSTNAME:-\$\(hostnamectl --static\)\}"$/ { f = 1 }
        f && /^NODE_NAME="\$HOSTNAME_NEW"$/ { exit }
        f { print }
    ' "$SCRIPT"
)"
if [[ -z "$RESOLVE_BLOCK" ]] \
    || ! grep -q 'read -r -p' <<<"$RESOLVE_BLOCK" \
    || ! grep -q 'die ' <<<"$RESOLVE_BLOCK"; then
    echo "FAIL - could not extract the resolve block from setup-jetson.sh"
    echo "       (anchor lines likely changed — update this test to match)"
    exit 1
fi

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

# Run the extracted block exactly as the real script does: under
# `set -euo pipefail`, with `hostnamectl --static` and `die` stubbed so the
# test is host-independent. stdin is supplied by the caller (a heredoc string
# for typed input, or /dev/null for the unattended/EOF re-run). The block ends
# by leaving the resolved name in HOSTNAME_NEW; we echo it. `die` prints the
# sentinel `__DIE__` so the required-hostname path is observable, and we mark a
# `set -e` abort (e.g. if `|| true` were dropped) with `__ABORT__`.
resolve_hostname() {
    local current_static="$1"        # what `hostnamectl --static` prints
    local override="${2-}"           # optional exported TURING_HOSTNAME ("" = unset)
    TURING_HOSTNAME="$override" bash -c '
        set -euo pipefail
        hostnamectl() { printf "%s\n" "'"$current_static"'"; }
        die() { echo "__DIE__"; exit 0; }
        '"$RESOLVE_BLOCK"'
        printf "%s\n" "$HOSTNAME_NEW"
    ' || echo "__ABORT__"
}

# First run: operator types a hostname → that value wins.
check "first run: typed value is used" \
    "jetson-1" "$(resolve_hostname "ubuntu" <<<"jetson-1")"

# Re-run, operator presses Enter → keeps the already-set hostname (no-op).
check "re-run: empty input keeps current hostname" \
    "jetson-1" "$(resolve_hostname "jetson-1" <<<"")"

# Re-run, fully unattended (stdin closed / EOF) → keeps current, does NOT hang
# or abort under set -e. This is the Slice J "clean no-op" requirement, and the
# case that the `|| true` after `read` protects: drop it and this run aborts
# (yielding __ABORT__ instead of the kept hostname), failing the test.
check "re-run: closed stdin keeps current hostname (no hang/abort)" \
    "jetson-1" "$(resolve_hostname "jetson-1" </dev/null)"

# First run on an unconfigured box where the operator just hits Enter and there
# is no usable current hostname → must still demand a value (die path).
check "first run: empty input + no current hostname dies" \
    "__DIE__" "$(resolve_hostname "" <<<"")"

# Operator explicitly renames an already-provisioned host → typed value wins.
check "re-run: explicit rename overrides current" \
    "jetson-2" "$(resolve_hostname "jetson-1" <<<"jetson-2")"

# TURING_HOSTNAME (exported by scripts/setup-fleet.sh) seeds the default so an
# unattended/EOF run names a fresh node with no prompt — even when the box still
# reports a generic current hostname.
check "fleet: TURING_HOSTNAME seeds the default on unattended run" \
    "jetson-9" "$(resolve_hostname "ubuntu" "jetson-9" </dev/null)"

# …but a hostname the operator actually types still overrides the env default.
check "fleet: typed input overrides TURING_HOSTNAME" \
    "jetson-3" "$(resolve_hostname "ubuntu" "jetson-9" <<<"jetson-3")"

if [[ "$fail" -ne 0 ]]; then
    echo "RESULT: FAIL"
    exit 1
fi
echo "RESULT: PASS"
