#!/usr/bin/env bash
# Regression test for the NATS .env resolve block in scripts/setup-jetson.sh
# (ADR 0010 §9, Slice H — TURING_NATS_URL + TURING_NATS_NKEY_SEED).
#
# The full setup script can only run on a real Jetson, so this exercises the
# load-bearing kernel in isolation: on a FRESH install the two NATS fields must
# be resolved (prompt, or env-var fallback for non-interactive runs) and a
# missing value must abort with a clear error; on a RE-RUN (an .env already
# present) the block must PRESERVE the existing values — never prompt, never
# clobber — and must not hang or abort on a closed stdin.
#
# Rather than mirror the logic, we extract the REAL resolve block from the
# shipped script and run it under the same `set -euo pipefail`, with `die` and
# `read`'s stdin stubbed. So:
#   (a) any edit to the NATS block in setup-jetson.sh is exercised here, and
#   (b) the set -e/EOF interaction is real — dropping the `|| true` after a
#       `read` would let a closed-stdin re-run abort under set -e, which this
#       test would catch as a FAIL.
#
# Run: bash tests/scripts/test_setup_jetson_env_nats.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../../scripts/setup-jetson.sh"

[[ -r "$SCRIPT" ]] || { echo "FAIL - cannot read $SCRIPT"; exit 1; }

# Pull the real resolve block out of the shipped script, between two stable,
# unique anchor lines: from the `ENV_PATH=` assignment (which the block uses to
# decide prompt-vs-preserve) up to but not including the Phase 1 banner. If the
# block can't be found the anchors drifted — fail loudly so the test is updated
# alongside the script.
RESOLVE_BLOCK="$(
    awk '
        /^ENV_PATH=\/home\/turing\/turing\/\.env$/ { f = 1 }
        f && /^# ── Phase 1: system provisioning/ { exit }
        f { print }
    ' "$SCRIPT"
)"
if [[ -z "$RESOLVE_BLOCK" ]] \
    || ! grep -q 'TURING_NATS_URL' <<<"$RESOLVE_BLOCK" \
    || ! grep -q 'TURING_NATS_NKEY_SEED' <<<"$RESOLVE_BLOCK" \
    || ! grep -q 'read -r -p' <<<"$RESOLVE_BLOCK" \
    || ! grep -q 'die ' <<<"$RESOLVE_BLOCK"; then
    echo "FAIL - could not extract the NATS resolve block from setup-jetson.sh"
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
# `set -euo pipefail`, with `die` stubbed (prints sentinel `__DIE__`) so the
# required-field path is observable and host-independent. We override ENV_PATH
# to point at either a missing path (fresh install → resolve/prompt) or an
# existing file (re-run → preserve). After the block we echo the resolved
# URL+seed (joined by `|`); on the preserve path those vars are never set, so
# we print `__PRESERVED__` to prove the block did NOT prompt or assign. A
# `set -e` abort (e.g. if a `|| true` were dropped) is marked `__ABORT__`.
resolve_nats() {
    local env_path="$1"; shift   # path the block tests with `[[ -f ... ]]`
    # remaining args become the exported environment for the inner shell
    # fd 3 carries the test result back out, untouched; the block's own
    # informational stdout (the "keeping existing" notice on the preserve
    # branch) is sent to /dev/null so only our assertions surface.
    # The inner script is intentionally single-quoted so $VARs expand in the
    # spawned shell, not here; $RESOLVE_BLOCK / $env_path are spliced in by
    # breaking out of the quotes. SC2016 is therefore expected, not a bug.
    # shellcheck disable=SC2016
    env "$@" bash -c '
        set -euo pipefail
        exec 3>&1
        # die reports through fd 3 (not the silenced fd 1) so the
        # required-field path stays observable.
        die() { echo "__DIE__" >&3; exit 0; }
        ENV_PATH_OVERRIDE="'"$env_path"'"
        { '"$RESOLVE_BLOCK"'
        } >/dev/null
        # The block sets ENV_PATH itself; force it back to our override so the
        # `[[ -f "$ENV_PATH" ]]` branch reflects the case under test.
        if [[ -n "${NATS_URL_NEW:-}" || -n "${NATS_NKEY_SEED_NEW:-}" ]]; then
            printf "%s|%s\n" "${NATS_URL_NEW:-}" "${NATS_NKEY_SEED_NEW:-}" >&3
        else
            printf "%s\n" "__PRESERVED__" >&3
        fi
    ' _ "ENV_PATH=$env_path" || echo "__ABORT__"
}

# The block opens with `ENV_PATH=/home/turing/turing/.env`, which would clobber
# our override. Re-run after stripping that first assignment so the test drives
# the branch it intends; everything else in the block is exercised verbatim.
RESOLVE_BLOCK="$(grep -v '^ENV_PATH=/home/turing/turing/\.env$' <<<"$RESOLVE_BLOCK")"
RESOLVE_BLOCK="ENV_PATH=\"\$ENV_PATH_OVERRIDE\"
$RESOLVE_BLOCK"

MISSING="/nonexistent/turing/.env"   # forces the fresh-install branch
EXISTING="$(mktemp)"                  # forces the re-run / preserve branch
trap 'rm -f "$EXISTING"' EXIT

# Fresh install, operator types both values → those values win.
check "fresh: typed URL+seed are used" \
    "nats://surface.ts.net:4222|SUASEED1" \
    "$(printf 'nats://surface.ts.net:4222\nSUASEED1\n' | resolve_nats "$MISSING")"

# Fresh install, non-interactive: values exported in the environment, stdin
# closed (EOF). The env defaults must win without hanging or aborting.
check "fresh: env-var fallback used on closed stdin (non-interactive)" \
    "nats://coord.ts.net:4222|SUASEED2" \
    "$(resolve_nats "$MISSING" TURING_NATS_URL=nats://coord.ts.net:4222 TURING_NATS_NKEY_SEED=SUASEED2 </dev/null)"

# Fresh install, empty URL and nothing to fall back to → must die (required).
check "fresh: empty URL dies" \
    "__DIE__" \
    "$(printf '\n\n' | resolve_nats "$MISSING")"

# Fresh install, URL given but empty seed and no fallback → must die (required).
check "fresh: URL set but empty seed dies" \
    "__DIE__" \
    "$(printf 'nats://surface.ts.net:4222\n\n' | resolve_nats "$MISSING")"

# Re-run: an .env already exists → preserve. The block must NOT prompt or assign
# the NATS_*_NEW vars (we print __PRESERVED__), and must not hang/abort even with
# stdin closed. This is the ADR 0010 §9 "preserve existing values on re-run".
check "re-run: existing .env is preserved, no prompt/clobber (closed stdin)" \
    "__PRESERVED__" \
    "$(resolve_nats "$EXISTING" </dev/null)"

# Re-run preservation must hold even if env vars are exported — an operator
# re-imaging with stale exports must not overwrite a provisioned .env.
check "re-run: env vars do not override an existing .env" \
    "__PRESERVED__" \
    "$(resolve_nats "$EXISTING" TURING_NATS_URL=nats://stale.ts.net:4222 TURING_NATS_NKEY_SEED=SUSTALE </dev/null)"

if [[ "$fail" -ne 0 ]]; then
    echo "RESULT: FAIL"
    exit 1
fi
echo "RESULT: PASS"
