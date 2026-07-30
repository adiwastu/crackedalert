#!/usr/bin/env bash

# ==========================================
# SWITCH TO THE OLD STACK (bash + MT5)
# Stops the new Python bot, then starts the
# bash listener/checker services.
# ==========================================

set -uo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "run as root: sudo $0"
    exit 1
fi

echo "=> stopping new bot..."
systemctl stop cracked-bot.service 2>/dev/null
# Matches '/opt/.../bin/python -m crackedalert'. This script's own command
# line has no 'python' in it, so it cannot match (and kill) itself.
pkill -f "python.* -m crackedalert" 2>/dev/null && echo "   killed a foreground bot process"

# Give Telegram a moment to release the long-poll before the other bot grabs it.
sleep 2

echo "=> starting bash stack..."
systemctl start cracked-listener.service cracked-checker.service
sleep 1

echo
systemctl is-active --quiet cracked-listener.service \
    && echo "✅ cracked-listener: running" \
    || echo "❌ cracked-listener: NOT running"
systemctl is-active --quiet cracked-checker.service \
    && echo "✅ cracked-checker:  running" \
    || echo "❌ cracked-checker:  NOT running"

if pgrep -f "python.* -m crackedalert" > /dev/null; then
    echo "⚠️  a python bot is STILL running -- both will fight over Telegram"
else
    echo "✅ new bot: stopped"
fi

# The v2 bot moved existing alerts into SQLite and renamed the TSV to
# .imported, so the bash checker starts from an empty alert list. Anything
# set through the new bot will not fire while the old stack is in charge.
if [ -f /etc/cracked_alert/cracked_alerts.tsv.imported ]; then
    echo
    echo "⚠️  alerts live in the v2 SQLite db now (cracked.db)."
    echo "   the bash checker sees an empty cracked_alerts.tsv --"
    echo "   alerts you set in the new bot will NOT fire right now."
fi

echo
echo "logs: journalctl -u cracked-listener -f"
