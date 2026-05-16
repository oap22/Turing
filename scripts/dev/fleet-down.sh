#!/usr/bin/env bash
# Stop the local Turing fleet. Preserves volumes (ollama models, sqlite data).
# For a full nuke including volumes, run: docker compose down -v
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

echo "=== Turing fleet down ==="
docker compose down
echo "fleet stopped (volumes preserved)"
