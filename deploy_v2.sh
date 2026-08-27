#!/usr/bin/env bash

# ==========================================
# CRACKED ALERT v2 (cTrader) - DEPLOYMENT
# Single-service deploy: venv install -> smoke test ->
# cracked-bot restart + Caddy-hosted UI.
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

# 6. Frontend (static command-builder UI) via Caddy
# Caddy owns :80 on this box (fronts geararea at :8080), so we serve the
# UI from a fixed path under /var/www instead of running a second web
# server. The site block is idempotently appended to /etc/caddy/Caddyfile.
echo "=> Installing UI site into /var/www/crackedalert-ui..."
rm -rf /var/www/crackedalert-ui
mkdir -p /var/www/crackedalert-ui
cp -r "$REPO_DIR/frontend/." /var/www/crackedalert-ui/

# Stamp the package version into the served UI pages (they carry a
# __VERSION__ token in the header) so the page always shows the version
# of the bot that is actually deployed -- no manual UI edits needed.
UI_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' \
    "$REPO_DIR/src/crackedalert/__init__.py" | head -1)"
if [ -n "$UI_VERSION" ]; then
    sed -i "s/__VERSION__/$UI_VERSION/g" \
        /var/www/crackedalert-ui/ui.html \
        /var/www/crackedalert-ui/tv.html
    echo "=> UI stamped with version $UI_VERSION"
fi

# Migrate the legacy root-domain block (hotland3x3.my.id) if it was
# previously auto-managed, so a rerun moves the site to the subdomain
# instead of leaving a stale duplicate block behind.
legacy_marker='site hotland3x3.my.id (crackedalert-ui)'
if grep -qF "$legacy_marker" /etc/caddy/Caddyfile; then
    echo "=> Migrating legacy UI site block (hotland3x3.my.id) -> alert.hotland3x3.my.id"
    sed -i "/^# $legacy_marker -- auto-managed by deploy_v2.sh/,/^}/d" /etc/caddy/Caddyfile
fi

CADDY_MARKER='site alert.hotland3x3.my.id (crackedalert-ui)'
# Always rewrite the auto-managed alert.* site block from the repo template,
# so new routes (e.g. the /alert-status proxy) apply on every deploy instead
# of being skipped once the block already exists.
if grep -qF "$CADDY_MARKER" /etc/caddy/Caddyfile; then
    echo "=> Updating auto-managed UI site block in /etc/caddy/Caddyfile..."
    sed -i "/^# $CADDY_MARKER -- auto-managed by deploy_v2.sh/,/^}/d" /etc/caddy/Caddyfile
fi
echo "=> Appending UI site block to /etc/caddy/Caddyfile..."
{
    echo ""
    echo "# $CADDY_MARKER -- auto-managed by deploy_v2.sh"
    cat "$REPO_DIR/deploy/caddy-ui.caddyfile"
} >> /etc/caddy/Caddyfile

if command -v caddy >/dev/null 2>&1; then
    caddy validate --config /etc/caddy/Caddyfile
fi
systemctl reload caddy
echo "✅ UI live at http://alert.hotland3x3.my.id/ui.html"

# ==========================================
# Cleanup: disable leftover legacy units (no-op on fresh boxes).
# ==========================================
systemctl disable --now cracked-listener.service cracked-checker.service 2>/dev/null || true
echo "🪦 legacy units disabled (if present)."
