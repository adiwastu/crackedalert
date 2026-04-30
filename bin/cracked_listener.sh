#!/usr/bin/env bash

# ==========================================
# CRACKED ALERT SYSTEM - LISTENER DAEMON
# ==========================================

# --- CONFIG & PATHS ---
WORK_DIR="/etc/cracked_alert"
ENV_FILE="${WORK_DIR}/.env_cracked"
DB_FILE="${WORK_DIR}/cracked_alerts.tsv"
OFFSET_FILE="${WORK_DIR}/.tg_offset"

mkdir -p "$WORK_DIR"
touch "$DB_FILE"
touch "$OFFSET_FILE"

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

echo "[Cracked Listener] Started. Polling Telegram..."

# --- MAIN LONG-POLLING LOOP ---
while true; do
    # 1. ALWAYS force read the latest offset from disk before calling Telegram
    OFFSET=$(cat "$OFFSET_FILE" 2>/dev/null)
    [ -z "$OFFSET" ] && OFFSET=0

    # Fetch updates with a 100-second timeout to hold the connection open
    UPDATES=$(curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" \
        -d "offset=${OFFSET}" \
        -d "timeout=100")

    # Check if we got a valid JSON response with updates
    HAS_UPDATES=$(echo "$UPDATES" | jq -r '.ok')
    
    if [ "$HAS_UPDATES" == "true" ]; then
        # Parse updates using jq and iterate through them
        echo "$UPDATES" | jq -c '.result[] | select(.message.text != null) | {update_id: .update_id, chat_id: .message.chat.id, text: .message.text}' | while read -r ROW; do
            
            UPDATE_ID=$(echo "$ROW" | jq -r '.update_id')
            CHAT_ID=$(echo "$ROW" | jq -r '.chat_id')
            RAW_TEXT=$(echo "$ROW" | jq -r '.text')

            # Update offset to acknowledge message
            OFFSET=$((UPDATE_ID + 1))
            echo "$OFFSET" > "$OFFSET_FILE"

            # --- COMMAND ROUTER ---
            
            # Command: /alert
            if [[ "$RAW_TEXT" == /alert* ]]; then
                # Parse arguments: /alert <PRICE> [SYMBOL] [MESSAGE]
                read -r CMD TARGET_PRICE SYMBOL MSG <<< "$RAW_TEXT"
                
                # Validation
                if [ -z "$TARGET_PRICE" ]; then
                    send_msg "$CHAT_ID" "⚠️ <b>Usage:</b> /alert 2450.00 XAUUSD Approaching support"
                    continue
                fi

                # Defaults
                [ -z "$SYMBOL" ] && SYMBOL="XAUUSD"
                [ -z "$MSG" ] && MSG="Price target reached."
                
                # Fetch live price to lock in directional logic
                API_URL="https://api.hotland3x3.my.id/fetch_data_pos?symbol=${SYMBOL}&timeframe=M1&num_bars=1"
                LIVE_PRICE=$(curl -s "$API_URL" | jq -r '.[-1].close')

                if [ -z "$LIVE_PRICE" ] || [ "$LIVE_PRICE" == "null" ]; then
                    send_msg "$CHAT_ID" "❌ API Error: Could not fetch live price for $SYMBOL."
                    continue
                fi

                # Determine direction using awk for floating point math
                DIRECTION=$(awk -v live="$LIVE_PRICE" -v target="$TARGET_PRICE" 'BEGIN { if (live < target) print "CROSSING_UP"; else print "CROSSING_DOWN" }')
                
                # Generate a short 4-character alphanumeric ID safely (CPU leak patched)
                ALERT_ID=$(head -c 100 /dev/urandom | tr -dc 'A-Z0-9' | head -c 4)

                # Write to flat database (TSV format)
                echo -e "${ALERT_ID}\t${CHAT_ID}\t${SYMBOL}\t${TARGET_PRICE}\t${DIRECTION}\t${MSG}" >> "$DB_FILE"

                # Confirm to user
                send_msg "$CHAT_ID" "✅ <b>Cracked Alert Locked</b>\nID: <code>$ALERT_ID</code>\nTarget: $SYMBOL @ $TARGET_PRICE\nDirection: $DIRECTION\nCurrent: $LIVE_PRICE"
                echo "Logged Alert: $ALERT_ID | $SYMBOL | $TARGET_PRICE | $DIRECTION"

            # Command: /alerts (List active)
            elif [[ "$RAW_TEXT" == /alerts* ]]; then
                ACTIVE_COUNT=$(grep -c "^[A-Z0-9].*${CHAT_ID}" "$DB_FILE" 2>/dev/null || echo "0")
                if [ "$ACTIVE_COUNT" -eq 0 ]; then
                    send_msg "$CHAT_ID" "No active alerts."
                else
                    LIST_MSG="<b>Active Cracked Alerts:</b>\n\n"
                    while IFS=$'\t' read -r ID C_ID SYM TGT DIR C_MSG; do
                        if [ "$C_ID" == "$CHAT_ID" ]; then
                            LIST_MSG+="• <code>$ID</code>: $SYM @ $TGT ($DIR)\n"
                        fi
                    done < "$DB_FILE"
                    send_msg "$CHAT_ID" "$LIST_MSG"
                fi

            # Command: /cancel
            elif [[ "$RAW_TEXT" == /cancel* ]]; then
                DEL_ID=$(echo "$RAW_TEXT" | awk '{print $2}')
                if [ -z "$DEL_ID" ]; then
                    send_msg "$CHAT_ID" "⚠️ <b>Usage:</b> /cancel <ID>"
                    continue
                fi

                # Delete safely using sed in-place
                if grep -q "^${DEL_ID}\t${CHAT_ID}" "$DB_FILE"; then
                    sed -i "/^${DEL_ID}\t${CHAT_ID}/d" "$DB_FILE"
                    send_msg "$CHAT_ID" "🗑️ Alert <code>$DEL_ID</code> cancelled."
                else
                    send_msg "$CHAT_ID" "❌ ID <code>$DEL_ID</code> not found or doesn't belong to you."
                fi
            fi
        done
    fi

    # Small sleep to prevent aggressive spinning if curl fails
    sleep 1
done