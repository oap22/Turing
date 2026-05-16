#!/usr/bin/env bash
# Bring up the local 4-node Turing fleet via docker compose.
#
# Verifies .env exists, starts the stack detached, tails pi-alpha's logs
# briefly so first-boot errors surface, then prints the gateway URL.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [ ! -f .env ]; then
  echo "error: .env not found at $REPO_ROOT/.env" >&2
  echo "       copy .env.example and fill in TURING_DISCORD_TOKEN / TURING_ANTHROPIC_API_KEY" >&2
  exit 1
fi

echo "=== Turing fleet up ==="
# --build ensures we don't silently run a stale image that predates
# recent source changes (e.g. the gateway feature).
docker compose up -d --build

echo
echo "--- tailing turing-node-1 for 10s (Ctrl-C is safe; containers keep running) ---"
# Portable replacement for `timeout 10 ...` — `timeout` is GNU coreutils
# and not present on macOS by default. Background the follow, sleep,
# then kill the pid.
docker compose logs -f turing-node-1 &
LOG_PID=$!
sleep 10
kill "$LOG_PID" 2>/dev/null || true
wait "$LOG_PID" 2>/dev/null || true

# Pull gateway port from .env (fallback to default 8765 from .env.example).
GATEWAY_PORT="$(grep -E '^TURING_GATEWAY_PORT=' .env | cut -d= -f2- | tr -d '"' || true)"
GATEWAY_PORT="${GATEWAY_PORT:-8765}"

echo
echo "fleet ready — gateway: http://localhost:${GATEWAY_PORT}/"
echo "  follow logs:  docker compose logs -f turing-node-1"
echo "  tear down:    ./scripts/dev/fleet-down.sh"
