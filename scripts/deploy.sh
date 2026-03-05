#!/usr/bin/env bash
# Deploy Turing to a Raspberry Pi
set -euo pipefail

PI_HOST="${1:?Usage: deploy.sh <pi-host>}"
PI_USER="${PI_USER:-turing}"
REMOTE_DIR="/home/${PI_USER}/turing"

echo "=== Deploying Turing to ${PI_USER}@${PI_HOST}:${REMOTE_DIR} ==="

# Sync code
echo "[1/3] Syncing code..."
rsync -avz --delete \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.env' \
    --exclude 'data/' \
    --exclude 'models/' \
    --exclude '.venv' \
    --exclude '.mypy_cache' \
    --exclude '.pytest_cache' \
    --exclude '.ruff_cache' \
    ./ "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/"

# Install dependencies
echo "[2/3] Installing dependencies..."
ssh "${PI_USER}@${PI_HOST}" "cd ${REMOTE_DIR} && /home/${PI_USER}/venv/bin/pip install -e ."

# Restart service
echo "[3/3] Restarting service..."
ssh "${PI_USER}@${PI_HOST}" "sudo systemctl restart turing"

echo ""
echo "=== Deployed successfully! ==="
echo "Check status: ssh ${PI_USER}@${PI_HOST} 'sudo systemctl status turing'"
echo "View logs:    ssh ${PI_USER}@${PI_HOST} 'sudo journalctl -u turing -f'"
