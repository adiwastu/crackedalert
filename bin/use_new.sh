#!/usr/bin/env bash

# ==========================================
# SWITCH TO THE NEW STACK (python + cTrader)
# Stops the bash services and any stray bot,
# then runs the new bot in the FOREGROUND so
# logs are visible. Ctrl+C stops it.
#
#   --service   run via systemd instead of
#               the foreground (production)
# ==========================================

set -uo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "run as root: sudo $0"
    exit 1
fi

# Production venv if it exists (deploy_v2.sh builds it), else the dev one.
PYTHON="${CRACKED_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x /opt/crackedalert/venv/bin/python ]; then
        PYTHON=/opt/crackedalert/venv/bin/python
    else
        PYTHON=/opt/crackedalert-dev/bin/python
    fi
fi

if [ ! -x "$PYTHON" ]; then
    echo "❌ python not found at $PYTHON"
    echo "   set CRACKED_PYTHON=/path/to/venv/bin/python"
    exit 1
fi

echo "=> stopping bash stack..."
systemctl stop cracked-listener.service cracked-checker.service 2>/dev/null

echo "=> stopping any existing new bot..."
systemctl stop cracked-bot.service 2>/dev/null
pkill -f "python.* -m crackedalert" 2>/dev/null && echo "   killed a foreground bot process"

# Give Telegram a moment to release the long-poll, otherwise the new
# instance gets 409 Conflict from the one that just died.
sleep 2

for svc in cracked-listener cracked-checker; do
    if systemctl is-active --quiet "${svc}.service"; then
        echo "❌ ${svc} refused to stop -- aborting to avoid a Telegram conflict"
        exit 1
    fi
done

echo "✅ old stack stopped. using $PYTHON"
echo

if [ "${1:-}" = "--service" ]; then
    systemctl start cracked-bot.service
    sleep 1
    systemctl is-active --quiet cracked-bot.service \
        && echo "✅ cracked-bot: running (journalctl -u cracked-bot -f)" \
        || { echo "❌ cracked-bot failed to start"; journalctl -u cracked-bot -n 20 --no-pager; exit 1; }
    exit 0
fi

echo "starting bot in foreground -- Ctrl+C to stop"
echo "----------------------------------------------------------"
exec "$PYTHON" -m crackedalert
