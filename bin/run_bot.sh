#!/usr/bin/env bash

# ==========================================
# Launch the v2 bot with whichever venv exists.
# Shared by systemd (cracked-bot.service) and
# bin/use_new.sh so both agree on the python.
#
# Override with CRACKED_PYTHON=/path/to/bin/python
# ==========================================

set -uo pipefail

PYTHON="${CRACKED_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    for candidate in /opt/crackedalert/venv/bin/python \
                     /opt/crackedalert-dev/bin/python; do
        if [ -x "$candidate" ]; then
            PYTHON="$candidate"
            break
        fi
    done
fi

if [ ! -x "${PYTHON:-}" ]; then
    echo "❌ no crackedalert venv found."
    echo "   looked in /opt/crackedalert/venv and /opt/crackedalert-dev"
    echo "   set CRACKED_PYTHON=/path/to/venv/bin/python"
    exit 1
fi

exec "$PYTHON" -m crackedalert "$@"
