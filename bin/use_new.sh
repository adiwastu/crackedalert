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

RUN_BOT="$(cd "$(dirname "$0")" && pwd)/run_bot.sh"

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

echo "✅ old stack stopped."
echo

if [ "${1:-}" = "--service" ]; then
    if [ ! -x /usr/local/bin/crackedalert-run ]; then
        echo "=> installing launcher + unit (first run)..."
        install -m 755 "$RUN_BOT" /usr/local/bin/crackedalert-run
        install -m 644 "$(dirname "$RUN_BOT")/../systemd/cracked-bot.service" \
            /etc/systemd/system/
        systemctl daemon-reload
    fi
    systemctl start cracked-bot.service
    sleep 2
    if systemctl is-active --quiet cracked-bot.service; then
        echo "✅ cracked-bot: running in the background"
        echo "   logs:  journalctl -u cracked-bot -f"
        echo "   stop:  systemctl stop cracked-bot"
    else
        echo "❌ cracked-bot failed to start"
        journalctl -u cracked-bot -n 30 --no-pager
        exit 1
    fi
    exit 0
fi

echo "starting bot in foreground -- Ctrl+C to stop"
echo "(use --service to run it in the background instead)"
echo "----------------------------------------------------------"
exec "$RUN_BOT"
