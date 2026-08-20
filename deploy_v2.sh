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

# 4. Launcher + systemd unit
echo "=> Installing launcher and systemd unit..."
install -m 755 "$REPO_DIR/bin/run_bot.sh" /usr/local/bin/crackedalert-run
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

# 6. Frontend (static command-builder UI) via nginx
echo "=> Ensuring nginx + installing UI site..."
if ! command -v nginx >/dev/null 2>&1; then
    echo "   nginx not found -- installing..."
    apt-get update -qq
    apt-get install -y -qq nginx
fi
sed "s|%REPO_DIR%|$REPO_DIR|g" "$REPO_DIR/deploy/nginx-ui.conf" \
    > /etc/nginx/sites-available/crackedalert-ui
ln -sf /etc/nginx/sites-available/crackedalert-ui /etc/nginx/sites-enabled/crackedalert-ui
nginx -t
systemctl enable nginx
systemctl reload nginx
echo "✅ UI live at http://hotland3x3.my.id/ui.html"

# ==========================================
# CUTOVER (Phase 5): retire the bash stack.
# ==========================================
systemctl disable --now cracked-listener.service cracked-checker.service
echo "🪦 bash stack retired."
