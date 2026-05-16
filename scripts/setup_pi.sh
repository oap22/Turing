#!/usr/bin/env bash
# Provision a Raspberry Pi for Turing
set -euo pipefail

echo "=== Turing Pi Provisioning ==="

# System updates
echo "[1/7] Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Python 3.11
echo "[2/7] Installing Python 3.11..."
sudo apt install -y python3.11 python3.11-venv python3.11-dev

# Build tools
echo "[3/7] Installing build tools..."
sudo apt install -y build-essential libsqlite3-dev bubblewrap

# Create turing user
echo "[4/7] Creating turing user..."
sudo useradd -m -s /bin/bash turing || true

# Install Ollama
echo "[5/7] Installing Ollama..."
curl -fsSL https://ollama.com/install.sh | sh

# Setup venv
echo "[6/7] Setting up Python virtual environment..."
sudo -u turing python3.11 -m venv /home/turing/venv
sudo -u turing /home/turing/venv/bin/pip install --upgrade pip

# Firewall
echo "[7/7] Configuring firewall..."
sudo apt install -y ufw
sudo ufw allow ssh
# Mesh presence rides on NATS (ADR-0008); no dedicated discovery port needed.
sudo ufw --force enable

# Systemd service
sudo tee /etc/systemd/system/turing.service > /dev/null <<'SERVICE'
[Unit]
Description=Turing AI Assistant
After=network.target ollama.service

[Service]
Type=simple
User=turing
WorkingDirectory=/home/turing/turing
ExecStart=/home/turing/venv/bin/python -m turing
Restart=always
RestartSec=10
EnvironmentFile=/home/turing/turing/.env

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable turing

echo ""
echo "=== Pi provisioning complete! ==="
echo "Next steps:"
echo "  1. Create /home/turing/turing/.env with your configuration"
echo "  2. Deploy code with: scripts/deploy.sh <pi-host>"
echo "  3. Pull an Ollama model: ollama pull gemma3:1b"
echo "  4. Start the service: sudo systemctl start turing"
