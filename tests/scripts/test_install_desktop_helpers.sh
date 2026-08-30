#!/usr/bin/env bash
# Regression tests for scripts/install-desktop.sh's Linux helpers (#400).
#
# Two behaviors, both extracted from the SHIPPED script (same convention as
# test_gateway_unit_guard.sh — any edit to the real logic is caught here):
#
#   1. xdg_data_home(): per the XDG Base Directory spec, $XDG_DATA_HOME is
#      honored only when set AND absolute; empty or relative values fall back
#      to ~/.local/share (mirrors config.rs::xdg_config_dir).
#   2. ere_escape()/appimage_pattern(): the pgrep/pkill -f pattern used to
#      stop a running AppImage must match ONLY the AppImage process itself —
#      never `tail -f <path>.log`, an editor with the path in argv, or a
#      lookalike path exploiting `.` as a regex wildcard.
#
# Run: bash tests/scripts/test_install_desktop_helpers.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../../scripts/install-desktop.sh"
[[ -r "$SCRIPT" ]] || { echo "FAIL - cannot read $SCRIPT"; exit 1; }

# Extract a top-level `name() { ... }` function body from the shipped script.
extract_fn() {
    sed -n "/^$1() {/,/^}/p" "$SCRIPT"
}
for fn in xdg_data_home ere_escape appimage_pattern; do
    body="$(extract_fn "$fn")"
    if [[ -z "$body" ]]; then
        echo "FAIL - could not extract $fn() from install-desktop.sh (layout changed — update this test)"
        exit 1
    fi
    eval "$body"
done

failures=0
check() {  # check <description> <expected> <actual>
    if [[ "$2" == "$3" ]]; then
        echo "ok   - $1"
    else
        echo "FAIL - $1: expected [$2], got [$3]"
        failures=$((failures + 1))
    fi
}

# --- 1. xdg_data_home -------------------------------------------------------
HOME_SAVED="$HOME"
HOME=/home/o

unset XDG_DATA_HOME
check "unset XDG_DATA_HOME falls back" "/home/o/.local/share" "$(xdg_data_home)"

XDG_DATA_HOME="" \
    check "empty XDG_DATA_HOME is ignored" "/home/o/.local/share" "$(XDG_DATA_HOME="" xdg_data_home)"

check "relative XDG_DATA_HOME is ignored (XDG spec)" "/home/o/.local/share" \
    "$(XDG_DATA_HOME="relative/data" xdg_data_home)"

check "absolute XDG_DATA_HOME is honored" "/custom/data" \
    "$(XDG_DATA_HOME="/custom/data" xdg_data_home)"

HOME="$HOME_SAVED"

# --- 2. appimage_pattern ----------------------------------------------------
# pgrep/pkill -f match the pattern as an unanchored POSIX ERE against the full
# command line (argv joined by spaces); grep -E is the same regex dialect, so
# it stands in for the kernel-side match here.
dest="/home/o/.local/bin/turing.AppImage"
pat="$(appimage_pattern "$dest")"

matches() { printf '%s\n' "$1" | grep -qE "$pat"; }

m() { if matches "$1"; then echo yes; else echo no; fi; }

check "the AppImage itself (no args) matches" yes "$(m "$dest")"
check "the AppImage with arguments matches" yes "$(m "$dest --some-flag")"
check "tail -f on the app's log is NOT matched" no \
    "$(m "tail -f /home/o/.local/bin/turing.AppImage.log")"
check "an editor with the path in argv is NOT matched" no \
    "$(m "vim $dest")"
check "a longer path containing dest as substring is NOT matched" no \
    "$(m "${dest}.backup")"
# Load-bearing lookalike: with UNescaped dots this path matches (both '.'
# wildcards swallow the X's) — it only stays unmatched because of ere_escape.
check "'.' does not act as a wildcard after escaping" no \
    "$(m "/home/o/Xlocal/bin/turingXAppImage")"

# Escaping must be safe for every ERE metacharacter, not just '.'.
weird="/tmp/we(ird)+dir/turing.AppImage"
wpat="$(appimage_pattern "$weird")"
if printf '%s\n' "$weird" | grep -qE "$wpat"; then
    echo "ok   - metacharacter-laden path still matches itself"
else
    echo "FAIL - metacharacter-laden path no longer matches itself"
    failures=$((failures + 1))
fi

if [[ "$failures" -eq 0 ]]; then
    echo "RESULT: PASS"
    exit 0
else
    echo "RESULT: FAIL ($failures)"
    exit 1
fi
