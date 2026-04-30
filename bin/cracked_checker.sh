#!/usr/bin/env bash

# ==========================================
# CRACKED ALERT SYSTEM - WATCHDOG DAEMON
# ==========================================

WORK_DIR="/etc/cracked_alert"
ENV_FILE="${WORK_DIR}/.env_cracked"
DB_FILE="${WORK_DIR}/cracked_alerts.tsv"

# --- LOAD SECRETS ---
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
else
    echo "ERROR: .env_cracked not found at $ENV_FILE"
    exit 1
fi

# --- HELPER: SEND TELEGRAM MESSAGE ---
send_msg() {
    local CHAT_ID="$1"
    local MSG="$2"
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${CHAT_ID}" \
        -d text="${MSG}" \
        -d parse_mode="HTML" > /dev/null
}

echo "[Cracked Checker] Started. Watching the wire..."

# --- MAIN WATCHDOG LOOP ---
while true; do
    # 1. Skip if the database is completely empty
    if [ ! -s "$DB_FILE" ]; then
        sleep 5
        continue
    fi

    # 2. Get unique symbols currently being monitored to minimize API calls
    ACTIVE_SYMBOLS=$(awk -F'\t' '{print $3}' "$DB_FILE" | sort -u)

    for SYM in $ACTIVE_SYMBOLS; do
        
        # 3. Fetch live tick data for this symbol
        API_URL="https://api.hotland3x3.my.id/fetch_data_pos?symbol=${SYM}&timeframe=M1&num_bars=1"
        RESP=$(curl -s "$API_URL")
        
        LIVE_PRICE=$(echo "$RESP" | jq -r '.[-1].close')

        # Skip if API fails or returns null
        if [ -z "$LIVE_PRICE" ] || [ "$LIVE_PRICE" == "null" ]; then
            echo "Failed to fetch live price for $SYM. Skipping this cycle."
            continue
        fi

        # 4. Find all alerts matching this symbol and evaluate them
        # We read the DB and build a list of IDs to delete so we don't mess up the file while reading it
        TRIGGERED_IDS=()

        while IFS=$'\t' read -r ID CHAT_ID ROW_SYM TARGET DIRECTION MSG; do
            # Skip empty lines or unmatching symbols
            [ -z "$ID" ] || [ "$ROW_SYM" != "$SYM" ] && continue

            # Evaluate logic using awk for floating-point math
            IS_TRIGGERED=$(awk -v live="$LIVE_PRICE" -v target="$TARGET" -v dir="$DIRECTION" '
                BEGIN {
                    if (dir == "CROSSING_UP" && live >= target) print "YES";
                    else if (dir == "CROSSING_DOWN" && live <= target) print "YES";
                    else print "NO";
                }
            ')

            if [ "$IS_TRIGGERED" == "YES" ]; then
                echo "🔥 TRIGGERED: $ID | $SYM crossed $TARGET (Live: $LIVE_PRICE)"
                
                # Format the alert payload
                ALERT_TEXT="🚨 <b>CRACKED ALERT TRIGGERED</b> 🚨\n\n"
                ALERT_TEXT+="<b>Symbol:</b> $SYM\n"
                ALERT_TEXT+="<b>Target:</b> $TARGET\n"
                ALERT_TEXT+="<b>Live Price:</b> $LIVE_PRICE\n"
                ALERT_TEXT+="<b>Note:</b> $MSG"

                # Fire to Telegram
                send_msg "$CHAT_ID" "$ALERT_TEXT"

                # Mark ID for deletion
                TRIGGERED_IDS+=("$ID")
            fi

        done < "$DB_FILE"

        # 5. Clean up triggered alerts from the database
        for DEL_ID in "${TRIGGERED_IDS[@]}"; do
            # sed -i safely removes the specific row
            sed -i "/^${DEL_ID}\t/d" "$DB_FILE"
            echo "Removed alert $DEL_ID from database."
        done

    done

    # Rest the loop
    sleep 5
done