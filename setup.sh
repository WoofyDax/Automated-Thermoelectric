#!/bin/bash
# ============================================================
#  TestEquity TEC1 — Raspberry Pi Setup Script
#  One command to install everything
# ============================================================
set -e

echo "============================================"
echo " TEC1 Automated Thermoelectric Setup"
echo " Optimized for Raspberry Pi 3 Model B"
echo "============================================"
echo ""

# ── Install system dependencies ──
echo "[1/4] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-dev git

# ── Install Python packages ──
echo "[2/4] Installing Python packages..."
pip3 install --break-system-packages pymodbus flask flask-socketio eventlet

# ── Create systemd service ──
echo "[3/4] Installing systemd service..."
SERVICE_FILE="/etc/systemd/system/tequity-dashboard.service"
sudo tee "$SERVICE_FILE" > /dev/null << 'SERVICEEOF'
[Unit]
Description=TestEquity TEC1 Dashboard
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Automated-Thermoelectric
ExecStart=/usr/bin/python3 dashboard.py --pi
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable tequity-dashboard

# ── Fix serial permissions ──
echo "[4/4] Setting up serial port access..."
sudo usermod -a -G dialout pi 2>/dev/null || true
sudo usermod -a -G dialout $USER 2>/dev/null || true

echo ""
echo "============================================"
echo " ✓ Setup complete!"
echo ""
echo " To start the dashboard now:"
echo "   sudo systemctl start tequity-dashboard"
echo ""
echo " Dashboard:  http://<your-pi-ip>:5000"
echo " Status:     sudo systemctl status tequity-dashboard"
echo " Logs:       journalctl -u tequity-dashboard -f"
echo ""
echo " IMPORTANT:"
echo " - POWER and TEMP switches must be ON"
echo " - FTDI USB adapter should show as /dev/ttyUSB0"
echo " - If port changes, edit config.json"
echo "============================================"
