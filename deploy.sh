#!/usr/bin/env bash

# ==========================================
# CRACKED ALERT SYSTEM - DEPLOYMENT SCRIPT
# ==========================================

if [ "$EUID" -ne 0 ]; then
  echo "❌ Error: Please run as root (sudo ./deploy.sh)"
  exit 1
fi

echo "🚀 Deploying Cracked Alert System..."

# 1. Update codebase
echo "=> Pulling latest code..."
git pull origin main

# 2. Setup Configuration Directory
echo "=> Configuring state directory (/etc/cracked_alert)..."
mkdir -p /etc/cracked_alert
touch /etc/cracked_alert/cracked_alerts.tsv
touch /etc/cracked_alert/.tg_offset
chown -R root:root /etc/cracked_alert
chmod 700 /etc/cracked_alert

# Warn if .env is missing
if [ ! -f "/etc/cracked_alert/.env_cracked" ]; then
    echo "⚠️  WARNING: /etc/cracked_alert/.env_cracked is missing!"
    echo "   Please create it and add your TELEGRAM_BOT_TOKEN before starting services."
fi

# 3. Install Binaries
echo "=> Installing bin..."
install -m 755 bin/cracked_listener.sh /usr/local/bin/cracked_listener.sh
install -m 755 bin/cracked_checker.sh /usr/local/bin/cracked_checker.sh

# 4. Install Systemd Services
echo "=> Installing systemd services..."
install -m 644 systemd/cracked-listener.service /etc/systemd/system/
install -m 644 systemd/cracked-checker.service /etc/systemd/system/

# 5. Reload & Restart
echo "=> Reloading daemon..."
systemctl daemon-reload

echo "=> Enabling & Starting services..."
# Note: No timers used here. Both are continuous loop services.
systemctl enable cracked-listener.service
systemctl restart cracked-listener.service

systemctl enable cracked-checker.service
systemctl restart cracked-checker.service

echo "✅ Cracked Alert is live."
echo "   Check logs with: journalctl -u cracked-listener -f"