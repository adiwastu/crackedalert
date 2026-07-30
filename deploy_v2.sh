#!/usr/bin/env bash

# ==========================================
# CRACKED ALERT v2 (cTrader) - DEPLOYMENT
# Replaces deploy.sh at the Phase 5 cutover.
# Old bash stack (cracked-listener/checker) is NOT touched here
# until the CUTOVER block at the bottom is uncommented.
# ==========================================

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "❌ Error: Please run as root (sudo ./deploy_v2.sh)"
  exit 1
fi

APP_DIR="/opt/crackedalert"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🚀 Deploying Cracked Alert v2..."

# 1. Update codebase
echo "=> Pulling latest code..."
git -C "$REPO_DIR" pull origin main

# 2. State directory (shared with v1 on purpose: env file + alerts import)
echo "=> Ensuring /etc/cracked_alert..."
mkdir -p /etc/cracked_alert
chmod 700 /etc/cracked_alert

if [ ! -f /etc/cracked_alert/.env_cracked ]; then
    echo "⚠️  /etc/cracked_alert/.env_cracked is missing -- copy .env.example and fill it."
fi
if [ ! -f /etc/cracked_alert/tokens.json ]; then
    echo "⚠️  /etc/cracked_alert/tokens.json is missing -- run auth_setup.py on this VPS."
fi

# 3. Install app into a venv
echo "=> Installing into ${APP_DIR}..."
mkdir -p "$APP_DIR"
if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet "$REPO_DIR"

# 4. Systemd unit
echo "=> Installing systemd unit..."
install -m 644 "$REPO_DIR/systemd/cracked-bot.service" /etc/systemd/system/
systemctl daemon-reload

# 5. Smoke test before (re)starting the service
echo "=> Running connection smoke test..."
if "$APP_DIR/venv/bin/python" -m crackedalert --smoke; then
    echo "=> Smoke test passed. Starting service..."
    systemctl enable cracked-bot.service
    systemctl restart cracked-bot.service
    echo "✅ Cracked Alert v2 is live."
    echo "   Logs: journalctl -u cracked-bot -f"
else
    echo "❌ Smoke test FAILED -- service not (re)started."
    exit 1
fi

# ==========================================
# CUTOVER (Phase 5): uncomment to retire the bash stack.
# Only after the new bot verifiably works with the real token.
# ==========================================
# systemctl disable --now cracked-listener.service cracked-checker.service
# echo "🪦 bash stack retired."
